"""Daily image posts for Share Market Charts (Pillow-rendered PNGs).

All images are rendered under the channel's own identity ("Share
Market Charts") — never another channel's logo, name or handle.

NAMES ONLY, NO LOGOS (standing rule, 31-Oct-2026): no image carries
a company logo of any kind — not a real logo, not a filing logo,
not an initials badge. Companies appear by their ticker/name text
only. This keeps every image clean, professional and free of any
logo-rights or wrong-logo risk.

Renderers take the same data the text digest builders use, so an
image never invents a company, a rating or a figure the text post
would not carry. Every image post is sent with a short text caption
carrying the essentials, so the facts stay searchable. Each image
also carries a small education-only line, because images get
forwarded without their captions.
"""

from PIL import Image, ImageDraw, ImageFont

from card import font as card_font

NAVY = (18, 32, 62)
WHITE = (255, 255, 255)
LIGHT_BG = (245, 247, 250)
GREY = (90, 100, 115)
LIGHT_LINE = (220, 226, 232)
GREEN = (0, 128, 96)
RED = (180, 30, 30)
GREEN_LINE = (0, 128, 96)

RATING_COLORS = {
    "Excellent": (0, 128, 96),
    "Good": (0, 110, 160),
    "Positive": (0, 140, 140),
    "OK": (110, 140, 0),
    "Weak": (180, 90, 0),
    "Bad": (180, 30, 30),
}
# Dark-theme recap palette (the recap image follows the dark table
# style Arka pointed at, with our own branding, our own 6 ratings
# and name-only chips — never the reference channel's logo, handle,
# labels or company logos).
DARK_BG = (10, 14, 22)
DARK_CARD = (26, 32, 46)
DARK_LINE = (45, 55, 72)
DARK_TEXT = (240, 244, 248)
DARK_GREY = (160, 170, 185)
RATING_DARK = {
    "Excellent": (34, 197, 94),
    "Good": (56, 189, 248),
    "Positive": (45, 212, 191),
    "OK": (250, 204, 21),
    "Weak": (251, 146, 60),
    "Bad": (248, 113, 113),
    "Filed": (160, 170, 185),
}
RATING_ORDER = ["Excellent", "Good", "Positive", "OK", "Weak", "Bad"]


def _text_size(d, text, fnt):
    box = d.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def _center_text(d, cx, y, text, fnt, fill):
    w, _ = _text_size(d, text, fnt)
    d.text((cx - w // 2, y), text, font=fnt, fill=fill)


def _fit_font(d, text, size, bold=True, max_w=1000):
    """Shrink a font until the text fits max_w (long tickers like
    ADVENZYMES must not spill into neighbouring tiles)."""
    fnt = card_font(bold=bold, size=size)
    while size > 18 and _text_size(d, text, fnt)[0] > max_w:
        size -= 2
        fnt = card_font(bold=bold, size=size)
    return fnt


def _header(d, width, title, subtitle):
    d.line([(0, 168), (width, 168)], fill=GREEN_LINE, width=4)
    _center_text(d, width // 2, 36, title,
                 card_font(bold=True, size=72), NAVY)
    if subtitle:
        _center_text(d, width // 2, 122, subtitle,
                     card_font(bold=False, size=36), GREY)


def _footer(d, width, height):
    _center_text(d, width // 2, height - 62, "Share Market Charts",
                 card_font(bold=True, size=30), NAVY)
    _center_text(d, width // 2, height - 28,
                 "For education only — not investment advice.",
                 card_font(bold=False, size=22), GREY)


def _wrap_lines(d, text, fnt, max_w):
    """Greedy word wrap for a comma-separated company list."""
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if _text_size(d, trial, fnt)[0] <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_morning_image(date_label, tickers, out_path):
    """Morning "Today Earnings" grid: one tile per company reporting
    that day. tickers are NSE symbols, already sorted. NAME ONLY:
    no logo, no initials badge — the ticker text is the tile."""
    tickers = sorted(set(tickers))
    cols = 8 if len(tickers) <= 64 else 10
    margin, gap = 48, 16
    width = 1600
    tile_w = (width - 2 * margin - gap * (cols - 1)) // cols
    tile_h = 104
    rows = (len(tickers) + cols - 1) // cols if tickers else 1
    header_h, footer_h = 210, 90
    height = header_h + rows * tile_h + (rows - 1) * gap + footer_h
    img = Image.new("RGB", (width, height), LIGHT_BG)
    d = ImageDraw.Draw(img)
    _header(d, width, "TODAY EARNINGS",
            f"{date_label}  •  {len(tickers)} Companies")
    if not tickers:
        _center_text(d, width // 2, header_h + 40,
                     "No results scheduled for today.",
                     card_font(bold=False, size=36), GREY)
    for idx, ticker in enumerate(tickers):
        r, c = divmod(idx, cols)
        x = margin + c * (tile_w + gap)
        y = header_h + r * (tile_h + gap)
        d.rounded_rectangle([x, y, x + tile_w, y + tile_h],
                            radius=18, fill=WHITE,
                            outline=LIGHT_LINE, width=2)
        d.rounded_rectangle([x + 18, y, x + tile_w - 18, y + 6],
                            radius=3, fill=GREEN)
        _center_text(d, x + tile_w // 2, y + 34, ticker,
                     _fit_font(d, ticker, 30, bold=True,
                               max_w=tile_w - 20), NAVY)
    _footer(d, width, height)
    img.save(out_path)
    return out_path


def _draw_dark_chips(d, names, x, y, max_w):
    """Name-only chips on the dark recap table. Returns the height
    used (a grey dash when the column is empty). No logos."""
    row_h = 56
    if not names:
        d.text((x, y + 8), "—", font=card_font(bold=False, size=30),
               fill=DARK_GREY)
        return row_h
    gap_x, gap_y = 12, 12
    cx, cy = x, y
    for name in names:
        fnt = _fit_font(d, name, 28, bold=True, max_w=300)
        w, _ = _text_size(d, name, fnt)
        chip_w = w + 32
        if cx + chip_w > x + max_w and cx > x:
            cx = x
            cy += row_h + gap_y
        d.rounded_rectangle([cx, cy, cx + chip_w, cy + row_h],
                            radius=14, fill=DARK_CARD,
                            outline=DARK_LINE, width=2)
        d.text((cx + 16, cy + 12), name, font=fnt, fill=DARK_TEXT)
        cx += chip_w + gap_x
    return cy - y + row_h


def render_recap_image(date_label, by_rating, out_path):
    """Night recap as a dark table: rating rows × During Market /
    After Market columns, in the style Arka pointed at — but with
    Share Market Charts branding, our own 6-level ratings and
    name-only chips (no company logos, no other channel's marks).
    by_rating maps a rating to (during_names, after_names). Ratings
    with no companies are omitted. Unparsed filings appear under the
    "Filed" row with no rating — never a guessed rating."""
    rows = [(r, by_rating[r]) for r in RATING_ORDER if r in by_rating
            and (by_rating[r][0] or by_rating[r][1])]
    if "Filed" in by_rating and (by_rating["Filed"][0]
                                 or by_rating["Filed"][1]):
        rows.append(("Filed", by_rating["Filed"]))
    margin = 48
    width = 1600
    rating_w = 260
    col_gap = 24
    col_w = (width - 2 * margin - rating_w - 2 * col_gap) // 2
    header_h, footer_h = 210, 90
    probe = Image.new("RGB", (10, 10))
    pd = ImageDraw.Draw(probe)
    heights = []
    for rating, (during, after) in rows:
        hd = _draw_dark_chips(pd, during, 0, 0, col_w)
        ha = _draw_dark_chips(pd, after, 0, 0, col_w)
        heights.append(max(hd, ha, 56) + 32)
    col_head_h = 56
    body_h = col_head_h + sum(heights)
    height = header_h + body_h + footer_h
    img = Image.new("RGB", (width, height), DARK_BG)
    d = ImageDraw.Draw(img)
    total = sum(len(a) + len(b) for _, (a, b) in rows)
    d.line([(0, 168), (width, 168)], fill=GREEN_LINE, width=4)
    _center_text(d, width // 2, 36, "EARNINGS RECAP",
                 card_font(bold=True, size=72), DARK_TEXT)
    _center_text(d, width // 2, 122,
                 f"{date_label}  •  {total} Results",
                 card_font(bold=False, size=36), DARK_GREY)
    # Table frame.
    tx0, tx1 = margin, width - margin
    ty0 = header_h
    ty1 = header_h + body_h
    col_x = margin + rating_w + col_gap
    d.rectangle([tx0, ty0, tx1, ty1], outline=DARK_LINE, width=2)
    d.line([(col_x - col_gap // 2, ty0),
            (col_x - col_gap // 2, ty1)], fill=DARK_LINE, width=2)
    d.line([(col_x + col_w + col_gap // 2, ty0),
            (col_x + col_w + col_gap // 2, ty1)], fill=DARK_LINE, width=2)
    d.line([(tx0, ty0 + col_head_h), (tx1, ty0 + col_head_h)],
           fill=DARK_LINE, width=2)
    d.text((margin + 16, ty0 + 10), "Rating",
           font=card_font(bold=True, size=30), fill=DARK_GREY)
    d.text((col_x, ty0 + 10), "During Market",
           font=card_font(bold=True, size=30), fill=DARK_GREY)
    d.text((col_x + col_w + col_gap, ty0 + 10), "After Market",
           font=card_font(bold=True, size=30), fill=DARK_GREY)
    y = ty0 + col_head_h
    for (rating, (during, after)), h in zip(rows, heights):
        color = RATING_DARK.get(rating, DARK_GREY)
        d.text((margin + 16, y + 16), rating.upper(),
               font=card_font(bold=True, size=30), fill=color)
        _draw_dark_chips(d, during, col_x, y + 16, col_w)
        _draw_dark_chips(d, after, col_x + col_w + col_gap, y + 16,
                         col_w)
        y += h
        if y < ty1:
            d.line([(tx0, y), (tx1, y)], fill=DARK_LINE, width=2)
    _center_text(d, width // 2, height - 62, "Share Market Charts",
                 card_font(bold=True, size=30), DARK_TEXT)
    _center_text(d, width // 2, height - 28,
                 "For education only — not investment advice.",
                 card_font(bold=False, size=22), DARK_GREY)
    img.save(out_path)
    return out_path


def render_wrap_image(date_label, nifty, breadth, sectors_up,
                      sectors_down, fii_net, dii_net, out_path):
    """Evening market wrap: NIFTY close, breadth, sector movers and
    a one-line FII/DII summary. All figures come from NSE; nothing
    here is estimated."""
    width, margin = 1600, 48
    header_h, footer_h = 210, 90
    body_h = 420
    height = header_h + body_h + footer_h
    img = Image.new("RGB", (width, height), LIGHT_BG)
    d = ImageDraw.Draw(img)
    _header(d, width, "MARKET WRAP", date_label)
    y = header_h
    value, chg, pct = nifty
    d.text((margin, y), "NIFTY 50",
           font=card_font(bold=True, size=34), fill=GREY)
    d.text((margin, y + 44), f"{value:,.2f}",
           font=card_font(bold=True, size=84), fill=NAVY)
    sign = "+" if chg >= 0 else ""
    chg_txt = f"{sign}{chg:,.2f}  ({sign}{pct:.2f}%)"
    d.text((margin, y + 150), chg_txt,
           font=card_font(bold=True, size=44),
           fill=GREEN if chg >= 0 else RED)
    adv, dec = breadth
    breadth_txt = f"{adv} advances  •  {dec} declines"
    bfnt = card_font(bold=False, size=34)
    bw, _ = _text_size(d, breadth_txt, bfnt)
    d.text((width - margin - bw, y + 30), breadth_txt,
           font=bfnt, fill=GREY)
    y += 240
    d.line([(margin, y), (width - margin, y)], fill=LIGHT_LINE, width=2)
    y += 24
    col_w = (width - 2 * margin - 32) // 2
    d.text((margin, y), "Sectors up",
           font=card_font(bold=True, size=32), fill=GREEN)
    if sectors_up:
        d.text((margin, y + 44),
               ", ".join(f"{n} {p:+.2f}%" for n, p in sectors_up),
               font=card_font(bold=False, size=32), fill=NAVY)
    d.text((margin + col_w + 32, y), "Sectors down",
           font=card_font(bold=True, size=32), fill=RED)
    if sectors_down:
        d.text((margin + col_w + 32, y + 44),
               ", ".join(f"{n} {p:+.2f}%" for n, p in sectors_down),
               font=card_font(bold=False, size=32), fill=NAVY)
    y += 120
    d.text((margin, y),
           f"FII net {fii_net}  •  DII net {dii_net}  (provisional)",
           font=card_font(bold=False, size=30), fill=GREY)
    _footer(d, width, height)
    img.save(out_path)
    return out_path


def render_fiidii_image(date_label, fii, dii, out_path):
    """Standalone FII/DII image: official NSE provisional cash-market
    figures. fii/dii are (buy, sell, net) in Rs crore."""
    width, margin = 1600, 48
    header_h, footer_h = 210, 90
    card_h = 300
    height = header_h + card_h + 40 + footer_h
    img = Image.new("RGB", (width, height), LIGHT_BG)
    d = ImageDraw.Draw(img)
    _header(d, width, "FII / DII ACTIVITY",
            f"{date_label}  •  Provisional, cash market")
    card_w = (width - 2 * margin - 32) // 2
    y = header_h
    for i, (title, (buy, sell, net)) in enumerate(
            [("FII / FPI", fii), ("DII", dii)]):
        x = margin + i * (card_w + 32)
        d.rounded_rectangle([x, y, x + card_w, y + card_h],
                            radius=20, fill=WHITE,
                            outline=LIGHT_LINE, width=2)
        d.text((x + 36, y + 24), title,
               font=card_font(bold=True, size=36), fill=NAVY)
        sign = "+" if net >= 0 else "-"
        d.text((x + 36, y + 80),
               f"Net {sign}₹{abs(net):,.0f} Cr",
               font=card_font(bold=True, size=56),
               fill=GREEN if net >= 0 else RED)
        d.text((x + 36, y + 170),
               f"Buy  ₹{buy:,.0f} Cr",
               font=card_font(bold=False, size=34), fill=GREY)
        d.text((x + 36, y + 220),
               f"Sell  ₹{sell:,.0f} Cr",
               font=card_font(bold=False, size=34), fill=GREY)
    _footer(d, width, height)
    img.save(out_path)
    return out_path


def render_actions_image(week_label, by_day, out_path):
    """Weekly corporate-actions image: board MEETINGS to consider
    dividend / bonus / buyback. by_day maps a date label to a list
    of symbols. These are meeting dates, not ex-dates."""
    width, margin = 1600, 48
    header_h, footer_h = 210, 90
    probe = Image.new("RGB", (10, 10))
    pd = ImageDraw.Draw(probe)
    fnt = card_font(bold=False, size=32)
    label_w = 220
    text_w = width - 2 * margin - label_w - 24
    heights = []
    for day, syms in by_day:
        lines = _wrap_lines(pd, ", ".join(syms), fnt, text_w)
        heights.append(max(1, len(lines)) * 44 + 24)
    height = header_h + sum(heights) + footer_h
    img = Image.new("RGB", (width, height), LIGHT_BG)
    d = ImageDraw.Draw(img)
    _header(d, width, "CORPORATE ACTIONS",
            f"{week_label}  •  Board meetings, not ex-dates")
    y = header_h
    for (day, syms), h in zip(by_day, heights):
        d.text((margin, y + 4), day,
               font=card_font(bold=True, size=32), fill=NAVY)
        lines = _wrap_lines(d, ", ".join(syms), fnt, text_w)
        for i, line in enumerate(lines):
            d.text((margin + label_w + 24, y + 4 + i * 44), line,
                   font=fnt, fill=GREY)
        y += h
    _footer(d, width, height)
    img.save(out_path)
    return out_path


def render_ipo_image(company, symbol, price_band, open_label,
                     close_label, listing_label, lot_label,
                     subscription, gmp_label, est_listing, out_path):
    """One IPO card. price_band / dates / subscription come from
    NSE; lot size, listing date and GMP come from public IPO
    trackers and are labelled unofficial — never presented as
    exchange data. Fields that are unavailable are omitted by the
    caller (pass None)."""
    width, margin = 1600, 48
    header_h, footer_h = 210, 90
    rows = [("Price Band", price_band),
            ("Open", open_label),
            ("Close", close_label),
            ("Listing", listing_label),
            ("Lot Size", lot_label),
            ("Subscription", subscription),
            ("GMP (unofficial)", gmp_label),
            ("Est. Listing", est_listing)]
    rows = [(k, v) for k, v in rows if v]
    body_h = 130 + len(rows) * 62 + 30
    height = header_h + body_h + footer_h
    img = Image.new("RGB", (width, height), LIGHT_BG)
    d = ImageDraw.Draw(img)
    _header(d, width, "IPO", symbol)
    y = header_h
    d.text((margin, y), company,
           font=_fit_font(d, company, 56, bold=True,
                          max_w=width - 2 * margin), fill=NAVY)
    y += 100
    for key, val in rows:
        d.text((margin, y), key,
               font=card_font(bold=True, size=34), fill=GREY)
        d.text((margin + 420, y), val,
               font=card_font(bold=True, size=34), fill=NAVY)
        y += 62
    _footer(d, width, height)
    img.save(out_path)
    return out_path
