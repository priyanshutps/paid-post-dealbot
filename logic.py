"""Pure helper logic (no Telegram calls) so it can be unit-tested."""
import re
from datetime import datetime, timedelta

URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.I)
TIME_RE = re.compile(r"(?<![\d@₹])(\d{1,2})\s*[:.]\s*(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?(?!\d)", re.I)
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
        return None
    return found[-1] if last else found[0]


def is_header(line):
    l = line.lower()
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
    for s2 in lines:
        if re.search(r"@\s*[₹\d]", s2):
            return s2
    return lines[0] if lines else ""


def _clean(s):
    s = re.sub(r"[^\w\s&,()\-:+.'/%]", " ", s)  # drop emojis / odd symbols
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" :-|,.")


def keyword_levels(text):
    """Three search phrases, longest first, like a human would try."""
    title = deal_title(text)
    l1 = _clean(title.split("@")[0])
    if not l1:
        l1 = _clean(title)
    # level 2: drop a short 'GRAB :' style prefix, stop at first comma / bracket
    body = l1
    if ":" in body:
        pre, post = body.split(":", 1)
        if len(pre.split()) <= 2 and post.strip():
            body = post
    l2 = _clean(re.split(r"[,(\[|]", body)[0])
    words = l2.split()
    if l2 == l1 and len(words) > 4:
        l2 = " ".join(words[:4])
    # level 3: first two meaningful words
    meaningful = [w for w in l2.split() if w.lower().strip(".,:") not in STOP | WEAK and len(w) > 1
                  and not re.search(r"\d+%|^\d+$", w)]
    l3 = " ".join(meaningful[:2]) if len(meaningful) >= 2 else l2
    levels = []
    for k in (l1, l2, l3):
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
