# Paid Post Deal Bot

Telethon userbot that automates the Plan C paid-post screenshot work:

1. Watches **Paid Post - Plan C** for deals (`Plan C - 10:35 PM @all ...`). Ignores bots (Rose tag repost).
2. At deal time **+1 min** sends `/c <keywords>` to **@screenshotslink_bot** (test search).
3. Checks the result: no "Not posted", all 9 channels found, each post is the same deal (text match) and posted within −20 / +60 min of the deal time (opens the posts when it can, and OCRs the bot screenshot for time + text).
4. **Asks you first** in Saved Messages: reply `/deal ok <id>` to send or `/deal no <id>` to skip (no reply in 15 min = skipped). Turn off with `ASK_APPROVAL=false` once you trust it.
5. If OK → sends the same `/c <keywords>` in **Paid Post Screenshot & Links**, checks again, presses **Send to Plan C Group (WhatsApp)**.
6. If not OK → tries again after 5 min with shorter keywords (3 tries: full line → product name → 2 words). After 3 failures, sends the deal to **Saved Messages** with "Not posted by everyone".
7. Follows edits, replies like "time changed 10:45 PM", and skips deals another admin already sent to WhatsApp in the last 30 min.

Understands: `Plan C - 10:35 PM @all`, `11:20 @all`, `9.15 @all`, `8:10 PM With Image @all`, `Free Post - 9:55 PM @all`,
`Post ASAP @all` / `dalo @all` (instant), multi-deal messages (`Plan C - 4 Deals @all` + `9:40 PM ✅` lines → one job each, ids like `#9957-2`),
deals without a time (waits for a reply with the time or "dalo"), and checks the @price so the same product at another price isn't accepted.

Every result (success / fail with log) is sent to your **Saved Messages**.
Control from Saved Messages: `/deal status`, `/deal pause`, `/deal resume`.

## Setup
1. my.telegram.org → API development tools → copy `API_ID`, `API_HASH`.
2. On your PC: `pip install telethon` then `python gen_session.py` → copy `SESSION_STRING`.
3. Railway → service Variables → add `API_ID`, `API_HASH`, `SESSION_STRING`.
4. (Optional) Railway → add a Volume mounted at `/data` so finished deals are remembered across restarts.

## Variables
| name | default | meaning |
|---|---|---|
| PLANS | C | plans to watch, e.g. `C,B` |
| PLAN_C_CHAT | Paid Post - Plan C | group title or id |
| MAIN_GROUP | Paid Post Screenshot & Links | group title or id |
| PLAN_C_COUNT | 9 | channels the bot should find |
| SEARCH_DELAY_MIN | 1 | minutes after deal time |
| RETRY_GAP_MIN | 5 | minutes between tries |
| MAX_TRIES | 3 | tries before giving up |
| WINDOW_AFTER_MIN | 60 | max gap between deal time and channel post |
| MIN_SIMILARITY | 0.6 | how close the post text must be |
| DUP_WINDOW_MIN | 30 | skip if someone else sent it within this many minutes |
| ASK_APPROVAL | true | ask in Saved Messages before posting |
| DRY_RUN | false | `true` = check only, never post in main group |
