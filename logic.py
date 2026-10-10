"""Pure helper logic (no Telegram calls) so it can be unit-tested."""
import re
from datetime import datetime, timedelta

URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.I)
TIME_RE = re.compile(r"(?<![\d@₹])(\d{1,2})\s*[:.]\s*(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?(?!\d)", re.I)
ASAP_RE = re.compile(r"\basap\b|post\s+now|right\s+now|immediately", re.I)
STOP = {"grab", "loot", "deal", "deals", "offer", "buy", "now", "the", "a", "an", "of", "for",
        "and", "with", "at", "on", "in", "to", "@all", "all", "price", "only", "steal", "+",
        "coupon", "use", "apply", "max", "qnty", "qty", "single", "pc", "pcs"}
WEAK = {"upto", "up", "off", "flat", "min", "extra", "minimum", "just", "free", "set", "pack", "combo"}


def _candidates(h, m, ampm, ref):
    if ampm:
        pm = ampm.lower().startswith("p")
        h = h % 12 + (12 if pm else 0)
        hours = [h]
    elif h > 12 or h == 0:
        hours = [h % 24]
    else:
        hours = [h % 12, h % 12 + 12]
    out = []
    for hh in hours:
        for day in (0, 1):
            out.append((ref + timedelta(days=day)).replace(hour=hh, minute=m, second=0, microsecond=0))
    return out


def parse_time(text, msg_dt, last=False):
    """Return the scheduled datetime written in the message (or None).
    msg_dt = when the message was posted (tz-aware, IST)."""
    head = text.splitlines()
    # prefer a time on a line that has no link / price (the header line)
    lines = [l for l in head if l.strip()]
    order = [l for l in lines if not URL_RE.search(l)] + [l for l in lines if URL_RE.search(l)]
    found = []
    for line in order:
        clean = URL_RE.sub(" ", line)
        for mt in TIME_RE.finditer(clean):
            h, m, ampm = int(mt.group(1)), int(mt.group(2)), mt.group(3)
            if h > 23 or m > 59:
                continue
            if "." in mt.group(0) and not ampm:
                # "9.15 @all" is a time, "@1.50" is a price: accept dot-times only on a header-like line
                l = clean.lower()
                if not ("@all" in l or "plan" in l or "time" in l or len(clean.split()) <= 3):
                    continue
            cands = [c for c in _candidates(h, m, ampm, msg_dt) if c >= msg_dt - timedelta(minutes=30)]
            if cands:
                found.append(min(cands))
    if not found:
        if ASAP_RE.search(text):
            return msg_dt  # "Post ASAP @all" = post right now
        return None
    return found[-1] if last else found[0]


def is_header(line):
    l = line.lower()
    if ASAP_RE.search(line) and ("@all" in line.lower() or len(line.split()) <= 4):
        return True
    return bool(TIME_RE.search(URL_RE.sub(" ", line))) and ("@all" in l or "plan" in l or len(l.split()) <= 4) \
        or l.strip() in ("@all",)


def deal_title(text):
    """The product line: first non-header line with an @price, else the first real line."""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or is_header(s):
            continue
        s2 = URL_RE.sub("", s).strip()
        if len(s2) < 3:
            continue
        lines.append(s2)
    price = re.compile(r"@\s*[₹\d]")
    for s2 in lines:
        if price.search(s2):
            # "Loot For Needy | Lifelong Treadmill @5879" -> keep the part with the price
            parts = [p.strip() for p in s2.split("|") if p.strip()]
            for p in parts:
                if price.search(p) and len(p.split("@")[0].strip()) >= 3:
                    return p
            return s2
    return lines[0] if lines else ""


def _clean(s):
    s = re.sub(r"[^\w\s&,()\-:+.'’‘/%]", " ", s)  # drop emojis / odd symbols
    s = re.sub(r"[ \t]*\n[ \t]*", " ", s).strip()  # keep inner spacing as written
    return s.strip(" :-|,.")


def _meaningful(seg):
    return [w for w in re.findall(r"[A-Za-z0-9’']+", seg)
            if w.lower() not in STOP | WEAK and not re.fullmatch(r"\d+", w) and len(w) > 1]


def keyword_levels(text):
    """Three search phrases, longest first. Each is a continuous piece of the deal line,
    so the screenshot bot (text search) can still match it."""
    title = deal_title(text)
    l1 = _clean(title.split("@")[0]) or _clean(title)
    # level 2: drop a short 'GRAB :' style prefix, then the comma/bracket part with most real words
    body = l1
    if ":" in body:
        pre, post = body.split(":", 1)
        if len(pre.split()) <= 2 and post.strip():
            body = post.strip()
    segs = [x.strip(" :-|,.&") for x in re.split(r"[,(\[|]", body) if x.strip(" :-|,.&")]
    l2 = max(segs, key=lambda x: len(_meaningful(x))) if segs else body
    if len(l2.split()) > 5:
        l2 = " ".join(l2.split()[:5])
    if l2 == l1 and len(l2.split()) > 3:
        l2 = " ".join(l2.split()[:4])
    # level 3: first two neighbouring real words of the product name (brand + word)
    w = body.split()
    l3 = l2
    for i in range(len(w) - 1):
        if _meaningful(w[i]) and _meaningful(w[i + 1]) and w[i] == w[i].strip(",.:()&") \
                and w[i + 1].strip(",.:()&"):
            l3 = w[i] + " " + w[i + 1].rstrip(",.:()&")
            break
    levels = []
    for k in (l1, l2, l3):
        k = k.strip(" :-|,.&")
        if k and k not in levels:
            levels.append(k)
    while len(levels) < 3:
        levels.append(levels[-1] if levels else title[:30])
    return levels


def tokens(s):
    s = URL_RE.sub(" ", s.lower())
    return [t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP and (len(t) > 1 or t.isdigit())]


def similarity(deal_text, other):
    """Share of the deal-title tokens found in the other text (0..1)."""
    want = tokens(deal_title(deal_text) or deal_text)
    if not want:
        return 0.0
    have = set(tokens(other))
    # OCR / truncation tolerant: prefix match counts
    hit = sum(1 for t in want if t in have or any(h.startswith(t) or t.startswith(h) and len(h) >= 3 for h in have))
    return hit / len(want)


def parse_bot_caption(caption):
    caption = caption or ""
    links = re.findall(r"https?://t\.me/\S+", caption)
    m = re.search(r"Not posted\s*\((\d+)\)\s*:?\s*(.*)", caption, re.I)
    not_posted = int(m.group(1)) if m else 0
    missing = m.group(2).strip() if m else ""
    return links, not_posted, missing


def parse_link(link):
    """t.me/c/123/456 -> (-100123, 456) ; t.me/name/456 -> ('name', 456)"""
    m = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


OCR_TIME = re.compile(r"(\d{1,2})[:.](\d{2})\s*([AaPp])\.?\s*[Mm]")
OCR_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b")


def ocr_rows(image_path):
    """OCR the bot's screenshot into rows: [{'time': (h,m) or None, 'date': bool, 'text': str}]"""
    from PIL import Image, ImageOps
    import pytesseract
    im = Image.open(image_path).convert("L")
    im = im.resize((im.width * 2, im.height * 2))
    if sum(im.getdata()) / (im.width * im.height) < 110:  # dark theme -> invert
        im = ImageOps.invert(im)
    data = pytesseract.image_to_data(im, output_type=pytesseract.Output.DICT, config="--psm 11")
    words = []
    for i, w in enumerate(data["text"]):
        if w.strip():
            words.append((data["top"][i], data["left"][i], data["height"][i], w))
    words.sort()
    # group into visual lines
    lines = []
    for top, left, h, w in words:
        if lines and abs(lines[-1]["top"] - top) <= max(8, h * 0.6):
            lines[-1]["words"].append((left, w))
        else:
            lines.append({"top": top, "words": [(left, w)]})
    for l in lines:
        l["text"] = " ".join(w for _, w in sorted(l["words"]))
    # a row starts at each line that contains a time or a date stamp
    rows = []
    for l in lines:
        t = OCR_TIME.search(l["text"])
        d = OCR_DATE.search(l["text"])
        if t or d:
            ap = t.group(3).lower() if t else None
            hm = (int(t.group(1)) % 12 + (12 if ap == "p" else 0), int(t.group(2))) if t else None
            rows.append({"time": hm, "date": bool(d and not t), "text": l["text"]})
        elif rows:
            rows[-1]["text"] += " " + l["text"]
    return rows


def in_window(post_dt, sched, before_min=20, after_min=60):
    return sched - timedelta(minutes=before_min) <= post_dt <= sched + timedelta(minutes=after_min)


# ------------------------------------------------------------------ multi-deal parsing
INSTANT_RE = re.compile(r"\b(asap|dalo|daalo|dal\s*do|daal\s*do|post\s+now|abhi\s+post|instant(ly)?|immediately|right\s+now)\b", re.I)
PRICE_RE = re.compile(r"@\s*₹?\s*(\d[\d,]*)")
EMOJI_JUNK = re.compile(r"[^\w\s:.@&()\-+/%|'’‘]")


def _time_in(line):
    """(hour, minute, ampm) of the first time in a line, or None."""
    clean = URL_RE.sub(" ", line)
    for mt in TIME_RE.finditer(clean):
        h, m, ampm = int(mt.group(1)), int(mt.group(2)), mt.group(3)
        if h > 23 or m > 59:
            continue
        if PRICE_RE.search(clean[max(0, mt.start() - 3):mt.end()]):
            continue  # "@1.99" is a price
        return h, m, ampm, mt
    return None


def is_time_line(line):
    """A line that only announces a time: 'Plan C - 10:35 PM @all', '9:07 PM (Same Time) ✅',
    '9.15 @all', '8:10 PM With Image @all', 'Free Post - 9:55 PM @all'."""
    if URL_RE.search(line) or PRICE_RE.search(line):
        return False
    t = _time_in(line)
    if not t:
        return False
    rest = EMOJI_JUNK.sub(" ", line[:t[3].start()] + " " + line[t[3].end():])
    words = [w for w in re.findall(r"[A-Za-z]+", rest)
             if w.lower() not in {"plan", "a", "b", "c", "all", "pm", "am", "same", "time", "with", "image",
                                  "free", "post", "at", "on", "deal", "deals", "new", "updated", "change", "changed"}]
    return len(words) <= 2


def is_instant_line(line):
    if URL_RE.search(line) or PRICE_RE.search(line):
        return False
    return bool(INSTANT_RE.search(line)) and len(line.split()) <= 6


def _resolve(h, m, ampm, msg_dt):
    cands = [c for c in _candidates(h, m, ampm, msg_dt) if c >= msg_dt - timedelta(minutes=30)]
    return min(cands) if cands else None


def parse_deals(text, msg_dt):
    """Split one plan message into deals.
    Returns [{'sched': datetime|None, 'instant': bool, 'text': str, 'title': str, 'price': str|None, 'label': str}]"""
    lines = (text or "").splitlines()
    segments, cur = [], {"time_line": None, "lines": []}
    instant_all = any(is_instant_line(l) for l in lines[:3])
    for l in lines:
        s = l.strip()
        if is_time_line(s):
            if cur["lines"] and any(URL_RE.search(x) for x in cur["lines"]):
                segments.append(cur)
                cur = {"time_line": s, "lines": []}
            else:
                cur["time_line"] = s  # header time (or time just before its deal)
            continue
        cur["lines"].append(l)
    segments.append(cur)
    deals = []
    for seg in segments:
        body = "\n".join(seg["lines"]).strip()
        if not URL_RE.search(body):
            continue
        sched, instant = None, False
        if seg["time_line"]:
            h, m, ap, _ = _time_in(seg["time_line"])
            sched = _resolve(h, m, ap, msg_dt)
        elif instant_all or any(is_instant_line(x) for x in seg["lines"][:3]):
            sched, instant = msg_dt, True
        seg_text = ((seg["time_line"] + "\n\n") if seg["time_line"] else "") + body
        title = deal_title(body)
        pm = PRICE_RE.search(title)
        deals.append({"sched": sched, "instant": instant, "text": seg_text, "title": title,
                      "price": pm.group(1).replace(",", "") if pm else None,
                      "label": seg["time_line"] or ("ASAP" if instant else "no time")})
    return deals


def price_ok(price, text):
    """False only when the text clearly shows a DIFFERENT @price."""
    if not price:
        return True
    found = [p.replace(",", "") for p in PRICE_RE.findall(text or "")]
    if not found:
        return True
    return price in found
