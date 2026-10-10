"""
Paid-post screenshot automation (Telethon userbot).

Flow for every deal posted in a "Paid Post - Plan X" group:
  1. read the time written in the deal (e.g. "Plan C - 10:35 PM @all")
  2. at that time + 1 min send "/c <keywords>" to @screenshotslink_bot (private chat) = test search
  3. check the bot result: every channel posted, same deal, posted around that time
  4. if correct -> send the same "/c <keywords>" in "Paid Post Screenshot & Links",
     check the result again, then press the "Send to ... (WhatsApp)" button
  5. if wrong -> wait 5 min, try shorter keywords; after 3 tries send the deal to
     Saved Messages with "Not posted by everyone"
It also follows edits / time-change replies in the plan group.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import logic as L

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealbot")
logging.getLogger("telethon").setLevel(logging.WARNING)

IST = timezone(timedelta(hours=5, minutes=30))


def env(name, default=None, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "on")
    return cast(v)


API_ID = env("API_ID", cast=int)
API_HASH = env("API_HASH")
SESSION_STRING = env("SESSION_STRING")

BOT_USERNAME = env("BOT_USERNAME", "screenshotslink_bot")
MAIN_GROUP = env("MAIN_GROUP", "Paid Post Screenshot & Links")
PLANS = [p.strip().upper() for p in env("PLANS", "C").split(",") if p.strip()]
PLAN_DEFAULTS = {  # command, channel count, group name
    "A": ("/a", 4, "Paid Post - Plan A"),
    "B": ("/b", 7, "Paid Post - Plan B"),
    "C": ("/c", 9, "Paid Post - Plan C"),
}
SEARCH_DELAY_MIN = env("SEARCH_DELAY_MIN", 1, float)
RETRY_GAP_MIN = env("RETRY_GAP_MIN", 5, float)
MAX_TRIES = env("MAX_TRIES", 3, int)
WINDOW_BEFORE_MIN = env("WINDOW_BEFORE_MIN", 15, int)   # post may come this early
WINDOW_AFTER_MIN = env("WINDOW_AFTER_MIN", 60, int)     # "under 1 hour" rule
MIN_SIMILARITY = env("MIN_SIMILARITY", 0.6, float)
DUP_WINDOW_MIN = env("DUP_WINDOW_MIN", 30, int)         # someone else sent it within this time = skip
BOT_REPLY_TIMEOUT = env("BOT_REPLY_TIMEOUT", 120, int)
DRY_RUN = env("DRY_RUN", False, bool)                   # true = never post in main group
ASK_APPROVAL = env("ASK_APPROVAL", True, bool)          # ask you in Saved Messages before posting
APPROVAL_WAIT_MIN = env("APPROVAL_WAIT_MIN", 15, float) # no answer in this time = skip
approvals = {}  # msg_id -> asyncio.Future
NOTIFY_SUCCESS = env("NOTIFY_SUCCESS", True, bool)
STATE_FILE = env("STATE_FILE", "/data/state.json" if os.path.isdir("/data") else "state.json")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

plans = {}        # chat_id -> {"key","cmd","count","entity"}
jobs = {}         # (chat_id, msg_id) -> job dict
running = set()
paused = False
bot_entity = None
main_entity = None


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("done", []))
    except Exception:
        return set()


done_keys = load_state()


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"done": sorted(done_keys)[-2000:]}, f)
    except Exception as e:
        log.warning("state save failed: %s", e)


def jkey(job):
    return f"{job['chat']}:{job['msg_id']}:{job['idx']}"


def now():
    return datetime.now(IST)


def fmt(dt):
    return dt.astimezone(IST).strftime("%d %b %I:%M %p")


async def notify(text):
    log.info("NOTIFY: %s", text.replace("\n", " | ")[:300])
    try:
        await client.send_message("me", text[:4000], parse_mode=None, link_preview=False)
    except Exception as e:
        log.warning("notify failed: %s", e)


# ---------------------------------------------------------------- resolving chats
async def resolve(name_or_id):
    s = str(name_or_id).strip()
    if re.fullmatch(r"-?\d+", s):
        return await client.get_entity(int(s))
    if s.startswith("@") or "t.me/" in s:
        return await client.get_entity(s)
    async for d in client.iter_dialogs():
        if (d.name or "").strip().lower() == s.lower():
            return d.entity
    raise RuntimeError(f"Chat not found in your dialogs: {s!r}")


# ---------------------------------------------------------------- plan-group watching
TIME_CHANGE_WORDS = re.compile(r"time|change|shift|delay|postpone|update|instead|new|now|reschedul", re.I)
ACTIVE = ("pending", "waiting", "running")


def make_job(chat_id, msg, idx, n, deal):
    job = {"chat": chat_id, "msg_id": msg.id, "idx": idx,
           "id": str(msg.id) if n == 1 else f"{msg.id}-{idx + 1}",
           "text": deal["text"], "price": deal["price"], "label": deal["label"],
           "sched": None, "attempt": 0, "next_run": None, "status": "waiting", "log": [],
           "created": msg.date.astimezone(IST)}
    if deal["sched"]:
        reschedule(job, deal["sched"])
    return job


def reschedule(job, sched):
    job["sched"] = sched
    job["attempt"] = 0
    job["next_run"] = sched + timedelta(minutes=SEARCH_DELAY_MIN)
    job["status"] = "pending"


def too_old(job):
    last_try = job["next_run"] + timedelta(minutes=RETRY_GAP_MIN * (MAX_TRIES - 1) + 10)
    return last_try < now()


def describe(job):
    when = fmt(job["next_run"]) if job["next_run"] else "⏳ waiting for a time"
    return f"#{job['id']} → search {when}\n   words: {L.keyword_levels(job['text'])[0]}"


async def is_bot_sender(msg):
    try:
        s = await msg.get_sender()
        return bool(getattr(s, "bot", False))
    except Exception:
        return False


def msg_jobs(chat_id, msg_id):
    return [j for k, j in sorted(jobs.items(), key=lambda kv: kv[0]) if k[0] == chat_id and k[1] == msg_id]


async def handle_plan_msg(msg, chat_id, startup=False, edited=False):
    text = msg.message or ""
    if not text.strip() or await is_bot_sender(msg):
        return  # ignore empty + bots (Rose re-posts the deal with tags)
    plan = plans[chat_id]
    P = f"Plan {plan['key']}"
    msg_dt = msg.date.astimezone(IST)
    deals = L.parse_deals(text, msg_dt)
    has_url = bool(L.URL_RE.search(text))

    # 1) edit of a message we already track (time / deal changed)
    existing = msg_jobs(chat_id, msg.id)
    if existing:
        changes = []
        for i, d in enumerate(deals):
            key = (chat_id, msg.id, i)
            j = jobs.get(key)
            if j is None:
                jobs[key] = make_job(chat_id, msg, i, len(deals), d)
                changes.append("new " + describe(jobs[key]))
                continue
            if j["status"] in ("done", "failed"):
                continue
            j["text"], j["price"], j["label"] = d["text"], d["price"], d["label"]
            if d["sched"] and d["sched"] != j["sched"]:
                reschedule(j, d["sched"])
                changes.append("time changed " + describe(j))
        if edited and not startup:
            handled = [j for j in existing if j["status"] in ("done", "failed")]
            note = (f"\n(already handled: {', '.join('#' + j['id'] for j in handled)})" if handled else "")
            await notify(f"✏️ {P} message edited\n" + "\n".join(changes or ["text updated"]) + note + f"\n\n{text}")
        return

    # 2) a reply to a tracked deal message -> time for it / time change / "dalo" now
    parents = [j for j in msg_jobs(chat_id, msg.reply_to_msg_id) if j["status"] in ACTIVE] \
        if msg.reply_to_msg_id else []
    if parents and not deals:
        t = L.parse_time(text, msg_dt, last=True)
        if not t and L.INSTANT_RE.search(text):
            t = msg_dt
        if t:
            waiting = [j for j in parents if j["status"] == "waiting"]
            targets = waiting or (parents if len(parents) == 1 else [])
            if targets:
                for j in targets:
                    reschedule(j, t)
                if not startup:
                    await notify(f"🔁 {P} time from reply:\n" + "\n".join(describe(j) for j in targets)
                                 + f"\n\nReply: {text}")
            elif not startup:
                await notify(f"⚠️ {P} reply with a time, but the message has {len(parents)} deals – "
                             f"I can't tell which one. Please check:\n\n{text}")
        return

    # 3) brand-new deal message (one or many deals)
    if deals:
        new = []
        for i, d in enumerate(deals):
            j = make_job(chat_id, msg, i, len(deals), d)
            if jkey(j) in done_keys:
                continue
            if j["next_run"] and (too_old(j) or j["sched"] - msg_dt > timedelta(hours=20)):
                continue
            if not j["next_run"] and now() - msg_dt > timedelta(hours=6):
                continue
            jobs[(chat_id, msg.id, i)] = j
            new.append(j)
            log.info("new job %s #%s sched=%s kw=%s", P, j["id"], j["sched"], L.keyword_levels(j["text"]))
        if new and not startup:
            head = f"🆕 {P}: {len(new)} deal(s) found" if len(new) > 1 else f"🆕 {P} deal scheduled"
            extra = ""
            if any(j["status"] == "waiting" for j in new):
                extra = "\n\n⏳ No time written – I'll wait for a reply with the time (or 'dalo'/'ASAP')."
            await notify(head + "\n" + "\n".join(describe(j) for j in new) + extra)
        return

    # 4) a plain "time changed" / "dalo" message (not a reply, no link)
    t = L.parse_time(text, msg_dt, last=True)
    if not t and L.INSTANT_RE.search(text) and len(text.split()) <= 8:
        t = msg_dt
    if t and not has_url and not startup and (TIME_CHANGE_WORDS.search(text) or L.INSTANT_RE.search(text)):
        active = [j for j in jobs.values() if j["chat"] == chat_id and j["status"] in ("pending", "waiting")]
        waiting = [j for j in active if j["status"] == "waiting"]
        target = waiting if len(waiting) == 1 else (active if len(active) == 1 else [])
        if target:
            reschedule(target[0], t)
            await notify(f"🔁 {P} time update → {describe(target[0])}\n\n{text}")
        elif active:
            await notify(f"⚠️ {P} time message, but {len(active)} deals are open – I can't tell which. "
                         f"Please check:\n\n{text}")


# ---------------------------------------------------------------- bot search + verification
def _final(m):
    t = (m.message or "").lower()
    return bool(m.photo or "t.me/" in t or "not posted" in t or "not found" in t
                or "no result" in t or "no post" in t or "no match" in t or "unauthori" in t
                or "error" in t or "timed out" in t)


search_lock = asyncio.Lock()


async def search(chat, cmd, in_group):
    async with search_lock:  # one search at a time so replies never get mixed up
        return await _search(chat, cmd, in_group)


async def _search(chat, cmd, in_group):
    sent = await client.send_message(chat, cmd)
    bot_id = bot_entity.id
    deadline = asyncio.get_event_loop().time() + BOT_REPLY_TIMEOUT
    found = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(3)
        msgs = await client.get_messages(chat, limit=15, min_id=sent.id)
        for m in reversed(msgs):
            if m.sender_id != bot_id:
                continue
            if in_group and m.reply_to_msg_id != sent.id:
                continue
            found = m
        if found and _final(found):
            await asyncio.sleep(4)  # let the bot finish editing
            return sent, await client.get_messages(chat, ids=found.id)
    return sent, found


def at_sched_day(sched, h, m):
    cands = [(sched + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0) for d in (-1, 0, 1)]
    return min(cands, key=lambda c: abs(c - sched))


async def verify(job, plan, botmsg):
    """Return (ok, report-lines)."""
    if botmsg is None:
        return False, ["bot did not reply"]
    caption = botmsg.message or ""
    links, not_posted, missing = L.parse_bot_caption(caption)
    rep = []
    if not_posted:
        return False, [f"Not posted ({not_posted}): {missing}"]
    if not links and not botmsg.photo:
        return False, [f"no results: {caption[:150]}"]
    if plan["count"] and len(links) < plan["count"]:
        return False, [f"only {len(links)}/{plan['count']} channels found"]

    sched = job["sched"]
    bad = []
    checked = 0
    # a) open each post directly (works for public channels / channels you are in)
    for link in links:
        ent, mid = L.parse_link(link)
        if ent is None:
            continue
        try:
            m = await client.get_messages(ent, ids=mid)
        except Exception:
            m = None
        if not m:
            continue
        checked += 1
        sim = L.similarity(job["text"], m.message or "")
        t_ok = L.in_window(m.date.astimezone(IST), sched, WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN)
        p_ok = L.price_ok(job.get("price"), m.message or "")
        if sim < MIN_SIMILARITY or not t_ok or not p_ok:
            bad.append(f"{link} → sim {sim:.0%}, posted {fmt(m.date)}" + ("" if p_ok else ", DIFFERENT PRICE"))
    # b) OCR the screenshot the bot made (time + first line of every channel)
    ocr_n = 0
    if botmsg.photo:
        try:
            with tempfile.TemporaryDirectory() as d:
                path = await client.download_media(botmsg, file=os.path.join(d, "r.jpg"))
                rows = L.ocr_rows(path)
            short_title = " ".join(L.tokens(L.deal_title(job["text"]))[:5])
            for r in rows:
                if r["date"]:
                    bad.append(f"old post (date shown): {r['text'][:60]}")
                    continue
                if not r["time"]:
                    continue
                ocr_n += 1
                pdt = at_sched_day(sched, *r["time"])
                if not L.in_window(pdt, sched, WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN):
                    bad.append(f"time {pdt.strftime('%I:%M %p')} too far: {r['text'][:50]}")
                elif L.similarity(short_title, r["text"]) < MIN_SIMILARITY - 0.1:
                    bad.append(f"different deal?: {r['text'][:70]}")
                elif not L.price_ok(job.get("price"), r["text"]):
                    bad.append(f"different price (deal @{job.get('price')}): {r['text'][:70]}")
        except Exception as e:
            log.warning("OCR failed: %s", e)
    rep.append(f"{len(links)} links, {checked} opened, {ocr_n} rows read from screenshot")
    if bad:
        return False, rep + bad
    if checked == 0 and ocr_n == 0:
        rep.append("⚠️ could not open posts or read screenshot – passed on bot result only")
    return True, rep


async def already_sent(job, plan, title_tokens):
    """Did someone else already send this deal to WhatsApp from the main group in the last ~30 min?"""
    since = now() - timedelta(minutes=DUP_WINDOW_MIN)
    title = set(title_tokens)
    msgs = await client.get_messages(main_entity, limit=80)
    for m in msgs:
        if m.date.astimezone(IST) < since:
            continue
        t = (m.message or "").strip()
        if not t.lower().startswith(plan["cmd"] + " "):  # includes our own earlier sends (restart safety)
            continue
        kw = set(L.tokens(t[len(plan["cmd"]):]))
        if not kw or not kw <= title:
            continue
        if len(kw) < 2 and len(title) > 2:  # "/c Lifelong" alone is too vague to call it the same deal
            continue
        for r in msgs:
            if r.reply_to_msg_id == m.id and "sent to" in (r.message or "").lower():
                s = await m.get_sender()
                who = "me (earlier run)" if m.out else (getattr(s, 'first_name', None) or 'someone')
                return f"{who} ('{t}' at {fmt(m.date)})"
    return None


async def press_whatsapp(chat, botmsg):
    botmsg = await client.get_messages(chat, ids=botmsg.id)
    if not botmsg or not botmsg.buttons:
        return False, "no button on bot message"
    texts = [b.text for row in botmsg.buttons for b in row]
    target = next((t for t in texts if "send to" in t.lower()), None) or \
        next((t for t in texts if "whatsapp" in t.lower() and "delete" not in t.lower()), None)
    if not target:
        return False, f"send button not found ({texts})"
    try:
        await botmsg.click(text=target)
    except Exception as e:
        log.info("click returned: %s", e)  # bots often answer late; check the edit instead
    for _ in range(15):
        await asyncio.sleep(2)
        m = await client.get_messages(chat, ids=botmsg.id)
        if m and "sent to" in (m.message or "").lower():
            return True, m.message.strip().split("\n")[0]
    return False, f"pressed '{target}' but no 'Sent to' confirmation"


async def run_attempt(job):
    plan = plans[job["chat"]]
    job["status"] = "running"
    try:
        fresh = await client.get_messages(plan["entity"], ids=job["msg_id"])
        if fresh is None:
            job["status"] = "failed"
            await notify(f"🗑 Plan {plan['key']} deal #{job['id']} was deleted – skipped.")
            return
        fd = L.parse_deals(fresh.message or "", fresh.date.astimezone(IST))
        if job["idx"] < len(fd) and fd[job["idx"]]["text"] != job["text"]:
            job["text"], job["price"] = fd[job["idx"]]["text"], fd[job["idx"]]["price"]
        levels = L.keyword_levels(job["text"])
        kw = levels[min(job["attempt"], len(levels) - 1)]
        cmd = f"{plan['cmd']} {kw}"
        log.info("attempt %d: %s", job["attempt"] + 1, cmd)

        _, res = await search(bot_entity, cmd, in_group=False)
        ok, rep = await verify(job, plan, res)
        job["log"].append(f"Try {job['attempt'] + 1} [{now().strftime('%I:%M %p')}] {cmd}\n  " + "\n  ".join(rep))

        if ok:
            who = await already_sent(job, plan, L.tokens(L.deal_title(job["text"])))
            if who:
                return await finish(job, True, f"already sent from main group by {who} – I didn't send again")
            if DRY_RUN:
                return await finish(job, True, f"DRY RUN: would send '{cmd}' in main group now")
            if ASK_APPROVAL:
                fut = asyncio.get_event_loop().create_future()
                approvals[job["id"]] = fut
                await notify(f"❓ Plan {plan['key']} deal #{job['id']} passed the test search.\n"
                             f"Send it to main group + WhatsApp with:  {cmd}\n\n"
                             f"Reply here:  /deal ok {job['id']}   or   /deal no {job['id']}\n"
                             f"(no answer in {APPROVAL_WAIT_MIN:g} min = skipped)\n\n"
                             + job["text"] + "\n\n" + "\n".join(rep))
                try:
                    yes = await asyncio.wait_for(fut, APPROVAL_WAIT_MIN * 60)
                except asyncio.TimeoutError:
                    yes = None
                approvals.pop(job["id"], None)
                if not yes:
                    return await finish(job, False, "skipped – " + ("you said no" if yes is False else "no approval reply"))
                who = await already_sent(job, plan, L.tokens(L.deal_title(job["text"])))
                if who:
                    return await finish(job, True, f"already sent from main group by {who} – I didn't send again")
            _, res2 = await search(main_entity, cmd, in_group=True)
            ok2, rep2 = await verify(job, plan, res2)
            job["log"].append("Main group check:\n  " + "\n  ".join(rep2))
            if ok2:
                who = await already_sent(job, plan, L.tokens(L.deal_title(job["text"])))
                if who:
                    return await finish(job, True, f"someone sent it meanwhile: {who} – I did NOT press WhatsApp")
                pressed, info = await press_whatsapp(main_entity, res2)
                if pressed:
                    return await finish(job, True, f"✅ {info}  (keywords: {kw})")
                return await finish(job, False, f"Checks OK but WhatsApp button failed: {info} – please press it manually")
        # failed this try
        job["attempt"] += 1
        if job["attempt"] >= MAX_TRIES:
            return await finish(job, False, "Not posted by everyone")
        job["next_run"] = now() + timedelta(minutes=RETRY_GAP_MIN)
        job["status"] = "pending"
    except Exception as e:
        log.exception("attempt crashed")
        job["attempt"] += 1
        if job["attempt"] >= MAX_TRIES:
            await finish(job, False, f"error: {e}")
        else:
            job["next_run"] = now() + timedelta(minutes=RETRY_GAP_MIN)
            job["status"] = "pending"


async def finish(job, success, summary):
    plan = plans[job["chat"]]
    job["status"] = "done" if success else "failed"
    done_keys.add(jkey(job))
    save_state()
    if success and not NOTIFY_SUCCESS:
        return
    head = ("✅ " if success else "❌ ") + f"Plan {plan['key']} – {fmt(job['sched'])} – {summary}"
    await notify(head + "\n\n" + job["text"] + "\n\n— log —\n" + "\n".join(job["log"]))


# ---------------------------------------------------------------- loop + control
async def scheduler():
    while True:
        try:
            if not paused:
                for job in list(jobs.values()):
                    if job["status"] == "pending" and job["next_run"] <= now():
                        asyncio.create_task(run_attempt(job))
                # forget finished jobs older than a day
                for k, j in list(jobs.items()):
                    ref = j["sched"] or j["created"]
                    if j["status"] in ("done", "failed") and ref < now() - timedelta(days=1):
                        jobs.pop(k, None)
                    elif j["status"] == "waiting" and j["created"] < now() - timedelta(hours=6):
                        jobs.pop(k, None)
        except Exception:
            log.exception("scheduler")
        await asyncio.sleep(10)


def status_text():
    act = [j for j in jobs.values() if j["status"] in ACTIVE]
    lines = [f"🤖 Deal bot {'PAUSED' if paused else 'running'} | plans {','.join(PLANS)} | dry_run={DRY_RUN} | ask_approval={ASK_APPROVAL}"]
    for j in sorted(act, key=lambda j: j["next_run"] or j["created"]):
        when = fmt(j["next_run"]) if j["next_run"] else "waiting for time"
        lines.append(f"• Plan {plans[j['chat']]['key']} #{j['id']} try {j['attempt'] + 1} – {when}: "
                     f"{L.deal_title(j['text'])[:45]}")
    if not act:
        lines.append("No pending deals.")
    return "\n".join(lines)


async def main():
    global bot_entity, main_entity, paused
    await client.start()
    me = await client.get_me()
    log.info("logged in as %s (%s)", me.first_name, me.id)
    bot_entity = await client.get_entity(BOT_USERNAME)
    main_entity = await resolve(MAIN_GROUP)
    for key in PLANS:
        cmd, count, name = PLAN_DEFAULTS.get(key, (f"/{key.lower()}", 0, f"Paid Post - Plan {key}"))
        ent = await resolve(env(f"PLAN_{key}_CHAT", name))
        count = env(f"PLAN_{key}_COUNT", count, int)
        cid = ent.id
        plans[cid] = {"key": key, "cmd": cmd, "count": count, "entity": ent}
    # events use marked ids
    from telethon.utils import get_peer_id
    marked = {get_peer_id(p["entity"]): cid for cid, p in plans.items()}

    def cid_of(event):
        return marked.get(event.chat_id)

    @client.on(events.NewMessage(chats=list(marked.keys())))
    async def on_new(event):
        await handle_plan_msg(event.message, cid_of(event))

    @client.on(events.MessageEdited(chats=list(marked.keys())))
    async def on_edit(event):
        await handle_plan_msg(event.message, cid_of(event), edited=True)

    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"(?i)^/deal(bot)?\b"))
    async def on_cmd(event):
        global paused
        t = event.raw_text.lower()
        m = re.search(r"\b(ok|yes|no|skip)\s+#?([\d-]+)", t)
        if m and m.group(2) in approvals:
            fut = approvals[m.group(2)]
            if not fut.done():
                fut.set_result(m.group(1) in ("ok", "yes"))
            await event.reply("👍 sending now" if m.group(1) in ("ok", "yes") else "⏭ skipped")
            return
        if "pause" in t:
            paused = True
        elif "resume" in t:
            paused = False
        await event.reply(status_text())

    # catch up on the last hours (restart safety)
    for cid, p in plans.items():
        msgs = [m async for m in client.iter_messages(p["entity"], limit=80)]
        for m in reversed(msgs):
            if m.date.astimezone(IST) > now() - timedelta(hours=12):
                await handle_plan_msg(m, cid, startup=True)
    await notify("🤖 Deal bot started.\n" + status_text() + "\n\nSend '/deal status', '/deal pause' or '/deal resume' here.")
    asyncio.create_task(scheduler())
    await client.run_until_disconnected()


if __name__ == "__main__":
    missing = [k for k in ("API_ID", "API_HASH", "SESSION_STRING") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")
    client.loop.run_until_complete(main())
