"""Professional PIL earnings card, branded "Share Market Charts".

Layout: navy header band (company + ticker badge + basis +
quarter), rating pill with the rule-based reason line, table
(QoQ, YoY, current, previous, year-ago) for Sales, Other Inc.,
Total Income, PAT, PAT Margin, EPS, three bar charts (Revenue, PAT,
EPS), footer with filing timestamp, education-only disclaimer and
channel name. All detail lives on the image itself, so the Telegram
caption can stay one short line. No operating-profit row is ever
invented: filings without a clean OP row show Total Income and
PAT Margin instead. PAT Margin = PAT / Revenue, as in the filings'
own ratio tables.
"""

import os
import re
from PIL import Image, ImageDraw, ImageFont

W = 1200
NAVY = (17, 28, 48)
NAVY_LIGHT = (30, 41, 59)
GREEN = (22, 163, 74)
SKY = (14, 165, 233)
LIME = (101, 163, 13)
ORANGE = (234, 88, 12)
RED = (220, 38, 38)
GREY = (100, 116, 139)
LIGHT_GREY = (226, 232, 240)
ROW_BG = (248, 250, 252)
BAR_OLD = (199, 210, 252)
BAR_NEW = (79, 70, 229)
WHITE = (255, 255, 255)
BLACK = (15, 23, 42)
PALE_BLUE = (224, 231, 255)

RATING_COLORS = {
    "Excellent": GREEN,
    "Good": GREEN,
    "Positive": SKY,
    "OK": LIME,
    "Weak": ORANGE,
    "Bad": RED,
}


def font(bold=False, size=40):
    for p in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"] if bold else
              ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"]):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def grp(v):
    neg = v < 0
    s = str(int(round(abs(v))))
    out = s[-3:]
    s = s[:-3]
    while s:
        out = s[-2:] + "," + out
        s = s[:-2]
    return ("-" if neg else "") + out


def _pct_str(p):
    if p is None:
        return "–"
    return f"{p:+.0f}%"


def _pct(a, b):
    if a is None or b is None or not b:
        return None
    return (a - b) / abs(b) * 100.0


def _wrap(d, text, fnt, max_w):
    lines, cur = [], ""
    for w in (text or "").split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _chart(d, box, title, values, labels, fmt):
    x, y, w, h = box
    d.rounded_rectangle([x, y, x + w, y + h], radius=18, outline=LIGHT_GREY, width=2)
    d.text((x + 20, y + 14), title, font=font(True, 26), fill=BLACK)
    top, bot = y + 78, y + h - 52
    ph = bot - top
    vmax = max(values)
    vmin = min(0, min(values))
    span = (vmax - vmin) or 1
    zero = top + ph * (vmax / span) if vmin < 0 else bot
    n = len(values)
    slot = (w - 40) / max(n, 1)
    bw = min(64, slot * 0.55)
    for i, (v, lab) in enumerate(zip(values, labels)):
        cx = x + 20 + slot * i + slot / 2
        if v >= 0:
            t = zero - (v / span) * ph
            d.rounded_rectangle([cx - bw / 2, t, cx + bw / 2, zero], radius=8,
                                fill=BAR_NEW if i == n - 1 else BAR_OLD)
            d.text((cx, t - 8), fmt(v), font=font(True, 22), fill=BLACK, anchor="mb")
        else:
            b = zero + (abs(v) / span) * ph
            d.rounded_rectangle([cx - bw / 2, zero, cx + bw / 2, b], radius=8, fill=RED)
            d.text((cx, b + 8), fmt(v), font=font(True, 22), fill=RED, anchor="ma")
        d.text((cx, bot + 10), lab, font=font(False, 22), fill=GREY, anchor="ma")


def render_card(data, out_path):
    """data: dict with company, ticker, basis, quarter_label, rating,
    reason, rows (list of (label, qoq, yoy, cur, prev, ly, qoq_color,
    yoy_color) where qoq/yoy are pre-formatted strings), charts,
    note, timestamp."""
    rows = data["rows"]
    note = data.get("note")
    reason_lines = _wrap(ImageDraw.Draw(Image.new("RGB", (10, 10))),
                         data.get("reason") or "", font(False, 27), W - 80)
    reason_lines = reason_lines[:2]
    dis_lines = _wrap(ImageDraw.Draw(Image.new("RGB", (10, 10))),
                      "For education only — not investment advice. "
                      "AI-generated summary. Verify with official filings.",
                      font(False, 23), W - 80)
    H = (150 + 26 + 56 + len(reason_lines) * 36 + 24
         + 60 + 6 * 64 + 30 + 300 + 30
         + (44 if note else 0) + 30 + 30 + len(dis_lines) * 30 + 28)
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    # Header band. NAME ONLY (global rule, 31-Oct-2026): no company
    # logo of any kind is drawn here, and no initials badge either —
    # the company name text is the header. The name auto-fits so a
    # long name never collides with the ticker badge.
    d.rectangle([0, 0, W, 150], fill=NAVY)
    name_x = 40
    badge = f" {data['ticker']} "
    fb = font(True, 30)
    bw = d.textlength(badge, font=fb) + 24
    name_size = 44
    name_fnt = font(True, name_size)
    while (name_size > 24 and d.textlength(data["company"], font=name_fnt)
           > W - 80 - bw - name_x):
        name_size -= 2
        name_fnt = font(True, name_size)
    d.text((name_x, 38), data["company"], font=name_fnt, fill=WHITE)
    d.text((name_x, 98), f"NSE filing · {data['basis']}",
           font=font(False, 27), fill=(203, 213, 225))
    d.rounded_rectangle([W - 40 - bw, 40, W - 40, 92], radius=12,
                        outline=WHITE, width=3)
    d.text((W - 40 - bw / 2, 66), badge, font=fb, fill=WHITE, anchor="mm")
    d.text((W - 40, 108), data["quarter_label"], font=font(True, 27),
           fill=WHITE, anchor="ra")

    # Rating pill + reason.
    ry = 150 + 26
    d.text((40, ry + 24), "Rating", font=font(False, 28), fill=GREY, anchor="lm")
    rcol = RATING_COLORS.get(data["rating"], GREY)
    pill = f" {data['rating']} "
    fp = font(True, 34)
    pw = d.textlength(pill, font=fp) + 28
    d.rounded_rectangle([150, ry, 150 + pw, ry + 52], radius=26, fill=rcol)
    d.text((150 + pw / 2, ry + 26), pill, font=fp, fill=WHITE, anchor="mm")
    d.text((W - 40, ry + 24), "₹ in Cr", font=font(False, 26), fill=GREY, anchor="rm")
    for i, ln in enumerate(reason_lines):
        d.text((40, ry + 52 + 12 + i * 36), ln, font=font(False, 27), fill=BLACK)

    # Table.
    ty = ry + 52 + 12 + len(reason_lines) * 36 + 24
    d.rounded_rectangle([30, ty, W - 30, ty + 60], radius=14, fill=NAVY)
    d.text((50, ty + 30), "Metric", font=font(True, 28), fill=WHITE, anchor="lm")
    for head, cx in [("QoQ", 470), ("YoY", 630),
                     (data["q_labels"][0], 830), (data["q_labels"][1], 990)]:
        d.text((cx, ty + 30), head, font=font(True, 28), fill=WHITE, anchor="mm")
    d.text((1155, ty + 30), data["q_labels"][2], font=font(True, 28),
           fill=WHITE, anchor="rm")
    for r, row in enumerate(rows):
        ry2 = ty + 60 + r * 64
        if r % 2 == 1:
            d.rectangle([30, ry2, W - 30, ry2 + 64], fill=ROW_BG)
        d.text((50, ry2 + 32), row[0], font=font(True, 30), fill=BLACK, anchor="lm")
        sz = 24 if row[0] == "PAT Margin" else 28
        d.text((470, ry2 + 32), row[1], font=font(True, sz), fill=row[6], anchor="mm")
        d.text((630, ry2 + 32), row[2], font=font(True, sz), fill=row[7], anchor="mm")
        for val, cx in [(row[3], 830), (row[4], 990)]:
            d.text((cx, ry2 + 32), val, font=font(cx == 830, 28), fill=BLACK, anchor="mm")
        d.text((1155, ry2 + 32), row[5], font=font(False, 28), fill=BLACK, anchor="rm")
        d.line([40, ry2 + 64, W - 40, ry2 + 64], fill=LIGHT_GREY, width=2)

    cy = ty + 60 + 6 * 64 + 30
    labels = [data["q_labels"][2], data["q_labels"][1], data["q_labels"][0]]
    ch = data["charts"]
    _chart(d, (30, cy, 373, 300), "REVENUE", ch["revenue"], labels, grp)
    _chart(d, (414, cy, 373, 300), "PAT", ch["pat"], labels, grp)
    _chart(d, (798, cy, 373, 300), "EPS", ch["eps"], labels, lambda v: f"{v:.2f}")

    fy2 = cy + 300 + 30
    if note:
        d.text((40, fy2), note, font=font(False, 26), fill=ORANGE)
        fy2 += 44
    d.line([40, fy2 + 6, W - 40, fy2 + 6], fill=LIGHT_GREY, width=2)
    d.text((40, fy2 + 12), data["timestamp"], font=font(False, 26), fill=GREY)
    d.text((W - 40, fy2 + 12), "Share Market Charts", font=font(True, 26),
           fill=BLACK, anchor="ra")
    for i, ln in enumerate(dis_lines):
        d.text((40, fy2 + 52 + i * 30), ln, font=font(False, 23), fill=GREY)

    img.save(out_path)
    return out_path


def card_data_from_parse(company, ticker, parsed, filing_time, q_labels):
    """Build render_card input from a parsed filing."""
    s, sp, sl = parsed["sales"]
    o = parsed["other"]
    t = parsed["total"]
    p, pp, pl = parsed["pat"]
    m, mp, ml = parsed["pat_margin"]
    e = parsed["eps"] or (None, None, None)

    def bps(a, b):
        if a is None or b is None:
            return "–"
        return f"{(a - b) * 100:+.0f} bps"

    def col(v):
        if v is None:
            return GREY
        return GREEN if v >= 0 else RED

    rows = [
        ("Sales", _pct_str(parsed["sales_qoq"]), _pct_str(parsed["sales_yoy"]),
         grp(s), grp(sp), grp(sl), col(parsed["sales_qoq"]), col(parsed["sales_yoy"])),
        ("Other Inc.", _pct_str(_pct(o[0], o[1])), _pct_str(_pct(o[0], o[2])),
         grp(o[0]), grp(o[1]), grp(o[2]),
         col(_pct(o[0], o[1])), col(_pct(o[0], o[2]))),
        ("Total Income", _pct_str(parsed["total_qoq"]), _pct_str(parsed["total_yoy"]),
         grp(t[0]), grp(t[1]), grp(t[2]), col(parsed["total_qoq"]), col(parsed["total_yoy"])),
        ("PAT", _pct_str(parsed["pat_qoq"]), _pct_str(parsed["pat_yoy"]),
         grp(p), grp(pp), grp(pl), col(parsed["pat_qoq"]), col(parsed["pat_yoy"])),
        ("PAT Margin", bps(m, mp), bps(m, ml),
         f"{m:.1f}%" if m is not None else "–",
         f"{mp:.1f}%" if mp is not None else "–",
         f"{ml:.1f}%" if ml is not None else "–", GREEN, GREEN),
        ("EPS", _pct_str(parsed["eps_qoq"]), _pct_str(parsed["eps_yoy"]),
         f"{e[0]:.2f}" if e[0] is not None else "–",
         f"{e[1]:.2f}" if e[1] is not None else "–",
         f"{e[2]:.2f}" if e[2] is not None else "–",
         col(parsed["eps_qoq"]), col(parsed["eps_yoy"])),
    ]
    return {
        "company": company, "ticker": ticker,
        "basis": parsed.get("basis", "Standalone"),
        "quarter_label": parsed["quarter_label"] or "Results",
        "rating": parsed["rating"], "reason": parsed.get("reason"),
        "rows": rows, "q_labels": q_labels,
        "charts": {"revenue": [sl, sp, s], "pat": [pl, pp, p],
                   "eps": [e[2] or 0, e[1] or 0, e[0] or 0]},
        "note": None, "timestamp": filing_time,
    }
