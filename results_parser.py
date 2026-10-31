"""Parse Indian quarterly results PDFs into structured numbers.

Proven lessons baked in (all from real NSE filings):
- CONSOLIDATED-FIRST (from 30-Oct-2026, user decision): a filing's
  consolidated results table is parsed when present, because that is
  what the market and other channels report. The consolidated
  statement heading is found first; if it is absent or its table
  refuses to parse, the parse falls back to the first table in the
  document (standalone). The returned dict carries "basis" so the
  card always labels which one it shows.
- The quarter label comes from the LATEST quarter-end date in the
  chosen table's own header block, never from the first month
  mentioned anywhere in the document (that mislabelled IGL's Q2
  FY27 card as "Q1 FY27").
- pdftotext/pypdf mangle numbers: split numbers ("2 1,759.40"),
  dropped decimal points ("94203" for 942.03, "063" for 0.63), and
  "(" sometimes read as "1". The numeric-section repair below handles
  the split and decimal-drop cases.
- Units come from the header above the revenue row (lakhs /100,
  millions /10, crore x1) and apply to money rows, never to EPS.
- The results table can sit hundreds of lines in, after a covering
  letter (CHEMBOND line 175, RESPONIND line 446), so the revenue row
  is searched across the whole document, not just the first 60 lines.
- Layout columns (2+ spaces apart) rebuild split numbers
  ("14 ,827 .89" -> 14,827.89); parentheses mean negative ("(71.93)"
  -> -71.93). PAT labels vary ("For the Period", "Net Profit after
  tax") and EPS headers vary ("Earning Per Share", "Earnings per
  share") -- all are tried.
- Total Income must equal Revenue + Other Income (within tolerance).
  On mismatch the parse is REFUSED -- the watcher then posts
  "Results Filed" with a link only, and never invents numbers.
"""

import re

MONEY_TOL = 1.0  # Rs Cr tolerance for the Total Income cross-check
MONEY_MAX = 5000000.0  # Rs Cr: no real quarterly figure is this big;
# a parse above it is mangled text (SJVN consolidated once read
# Sales as 1,032,403,638 Cr), so the parse is refused outright.

LAST_REFUSAL = None  # reason the latest segment parse refused, if any


def _refuse(reason):
    """Record why a segment refused, then return None.

    Behaviour-neutral: callers still see None; the watcher logs the
    reason so a "Results Filed" text post can be diagnosed instead
    of guessed at.
    """
    global LAST_REFUSAL
    LAST_REFUSAL = reason
    return None


def _normalize_mangled(s):
    """Undo systematic pypdf misreads inside a numeric token/chunk.

    Observed in real filings (IGL consolidated, pypdf text):
    - "I" for "1": "I0,658.82" is 10,658.82.
    - "4" for "-1": "5,043.-14" is 5,043.44 and "4,-145.96" is
      4,445.96 (in both cases "44" came out as "-14").
    A dot- or comma-followed "-1" is never a valid number, so the
    replacement cannot corrupt a genuine value; the Total Income
    cross-check still guards the current quarter.
    """
    if any(c.isdigit() for c in s):
        s = s.replace("I", "1").replace("l", "1")
    return s.replace(".-1", ".4").replace(",-1", ",4")


def _repair_numeric_tokens(tokens):
    """Repair mangled numeric tokens inside a table row's value section."""
    out = []
    for tok in tokens:
        tok = tok.strip("_—–~*=").rstrip("];")
        tok = _normalize_mangled(tok)
        neg = False
        if tok.startswith("(") and tok.endswith(")"):
            # "(184.04)" is a loss: parentheses are the minus sign.
            neg = True
            tok = tok[1:-1]
        elif tok.startswith("-"):
            neg = True
        t = tok.lstrip("-").replace(",", "")
        if re.fullmatch(r"\d\.\d{3}\.\d+", t):
            # "1.394.38" is 1,394.38 with a dot as thousands separator.
            t = t.replace(".", "", 1)
        if re.fullmatch(r"\d+\.\d+", t):
            v = float(t)
        elif re.fullmatch(r"0\d\d", t):
            # "063" -> 0.63 (decimal point dropped, leading zero kept)
            v = float(t[0] + "." + t[1:])
        elif re.fullmatch(r"\d{4,}", t):
            # "94203" -> 942.03, "217582" -> 2175.82
            v = float(t[:-2] + "." + t[-2:])
        elif re.fullmatch(r"\d+", t):
            # Bare small integer inside the value section: keep as-is;
            # only the first three values are used, so stray row numbers
            # after them do not matter.
            v = float(t)
        else:
            continue
        out.append(-v if neg else v)
    return out


def _parse_chunk(chunk):
    """Parse one column chunk into a float, or None if not a number.

    Handles parentheses negatives "(71.93)", leading minus, internal
    hyphen noise from bad OCR ("20,0-37.19" -> 20,037.19), dropped
    decimal points ("94203" -> 942.03, "063" -> 0.63), and grouped
    numbers whose decimal part lost its dot ("5411512" -> 54115.12).
    """
    s = chunk.replace(" ", "").replace("|", "")
    if not s:
        return None
    # OCR junk attached to a value (stray _, dashes, brackets, stars,
    # semicolons from bad OCR layers). Strip only this safe set; the
    # minus sign and parentheses are handled below, so signs survive.
    s = s.strip("_—–~*=")
    s = s.rstrip("];")
    s = s.lstrip("_—–~*=")
    if not s:
        return None
    s = _normalize_mangled(s)
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    elif s.startswith("-"):
        neg = True
        s = s[1:]
    # Hyphen between digits is OCR noise, not a minus sign.
    s = re.sub(r"(?<=\d)-(?=\d)", "", s)
    t = s.replace(",", "")
    if re.fullmatch(r"\d\.\d{3}\.\d+", t):
        # "1.394.38" is 1,394.38 with a dot as thousands separator.
        t = t.replace(".", "", 1)
    if re.fullmatch(r"\d+\.\d+", t):
        v = float(t)
    elif re.fullmatch(r"0\d\d", t):
        # "063" -> 0.63 (decimal point dropped, leading zero kept)
        v = float(t[0] + "." + t[1:])
    elif re.fullmatch(r"\d{4,}", t):
        # "94203" -> 942.03, "217582" -> 2175.82
        v = float(t[:-2] + "." + t[-2:])
    elif re.fullmatch(r"\d+", t):
        v = float(t)
    else:
        return None
    return -v if neg else v


def _row_values_tokens(line):
    """Legacy token-based value extraction (fallback for pypdf text
    without layout columns). Split fragments are joined first, on
    TOKEN boundaries only: a bare token joins the following decimal
    token only when it contains a comma ("22,4" + "17.29") or is at
    most 2 digits ("2" + "1,759.40"). A 3-digit token like "063" is
    a complete mangled value (0.63), not a fragment, so it is never
    joined. The value section then starts at the first decimal token.
    """
    line = line.replace("|", " ")
    toks = line.split()
    joined = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        stripped = tok.lstrip("-").replace(",", "")
        if (i + 1 < len(toks)
                and re.fullmatch(r"-?\d[\d,]*", tok)
                and "." not in tok
                and re.fullmatch(r"-?\d[\d,]*\.\d+", toks[i + 1])
                and len(stripped) <= 3
                and not stripped.startswith("0")):
            joined.append(tok + toks[i + 1])
            i += 2
        else:
            joined.append(tok)
            i += 1
    start = None
    for i, tok in enumerate(joined):
        stripped = tok.lstrip("-").replace(",", "")
        if (re.fullmatch(r"-?\d[\d,]*\.\d+", tok)
                or (re.fullmatch(r"-?\d[\d,]*", tok)
                    and len(stripped) >= 4)):
            # Decimal token, or a bare integer of 4+ digits: the
            # latter is a value whose decimal point was dropped
            # ("224432" is 2,244.32, "3,44658" is 3,446.58). Row
            # serials are 1-2 digits, so they never start the section.
            start = i
            break
    if start is None:
        return []
    return _repair_numeric_tokens(joined[start:])


def _row_values(line):
    """Extract the value section of a table row as floats.

    Layout text (pdftotext -layout) separates table columns with
    two or more spaces, while mangled numbers split on single
    spaces ("14 ,827 .89", "31,872. 17", "54 ,115 12"). Splitting
    on 2+ spaces first keeps each column intact, then removing the
    single spaces inside a column rebuilds the number. Falls back
    to the legacy token repair when the line has no layout columns
    (e.g. pypdf text).
    """
    parts = re.split(r"\s{2,}", line.strip())
    if len(parts) >= 4:
        vals = []
        # pypdf often glues the first value to the label with a
        # single space ("1 Revenue from operations 8,917.85"), so
        # the label part itself ends with a value. Recover it: a
        # trailing decimal or comma-grouped integer is a value, a
        # bare year ("2026") is not.
        m = re.search(r"(\(?-?[\d,]*\.\d+\)?|-?\d{1,3}(?:,\d{3})+)\s*$",
                      parts[0])
        if m:
            v = _parse_chunk(m.group(1))
            if v is not None:
                vals.append(v)
        for chunk in parts[1:]:
            v = _parse_chunk(chunk)
            if v is not None:
                vals.append(v)
        if len(vals) >= 3:
            return vals
    return _row_values_tokens(line)


def _find_unit_factor(text, revenue_pos):
    """Units from the header above the revenue row.

    OCR-batch fix: "lacs" is a real unit spelling (Rs. in Lacs) and
    does NOT contain "lakh", so the old substring check missed it
    and read lakh figures as crore (100x too big). The word-boundary
    regex below catches lakhs/lakh/lacs/lac.
    """
    head = text[max(0, revenue_pos - 3000):revenue_pos].lower()
    if re.search(r"\blakhs?\b|\blacs?\b", head):
        return 0.01
    if "million" in head:
        return 0.1
    return 1.0


def _first_match_line(lines, start, pattern, exclude=None):
    rx = re.compile(pattern, re.IGNORECASE)
    for i in range(start, min(len(lines), start + 60)):
        if rx.search(lines[i]):
            if exclude and any(e in lines[i].lower() for e in exclude):
                continue
            return i
    return None


def _sq(line):
    return re.sub(r"\s+", "", line.lower())


def _first_match_line_sq(lines, start, needles, exclude=None):
    """Whitespace-insensitive line search (fallback for mangled
    labels like "Tota l income" or "Profit for th e period", where
    pypdf split a word with a space)."""
    for i in range(start, min(len(lines), start + 60)):
        s = _sq(lines[i])
        if all(n in s for n in needles):
            if exclude and any(e in s for e in exclude):
                continue
            return i
    return None


def verdict(sales_yoy, pat_yoy):
    """6-level rule-based verdict + reason line (never an AI claim)."""
    if sales_yoy is not None and pat_yoy is not None:
        if sales_yoy > 0 and pat_yoy > 25:
            return "Excellent", f"Sales up {sales_yoy:.1f}% YoY, PAT up {pat_yoy:.1f}% YoY."
        if sales_yoy > 0 and pat_yoy > 0:
            return "Good", f"Sales up {sales_yoy:.1f}% YoY, PAT up {pat_yoy:.1f}% YoY."
        if pat_yoy > 0 or (sales_yoy > 0 and pat_yoy >= -10):
            return "Positive", f"Sales {sales_yoy:+.1f}% YoY, PAT {pat_yoy:+.1f}% YoY."
        if sales_yoy >= -5 and pat_yoy >= -10:
            return "OK", f"Sales {sales_yoy:+.1f}% YoY, PAT {pat_yoy:+.1f}% YoY."
        if pat_yoy >= -25:
            return "Weak", f"Sales {sales_yoy:+.1f}% YoY, PAT {pat_yoy:+.1f}% YoY."
        return "Bad", f"Sales {sales_yoy:+.1f}% YoY, PAT {pat_yoy:+.1f}% YoY."
    return "Results Filed", "Results filed."


def _pct(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return (cur / base - 1.0) * 100.0


def _pct_signed(cur, base):
    """YoY/QoQ for PAT: signed change against |base| so losses work."""
    if cur is None or base is None or base == 0:
        return None
    return (cur - base) / abs(base) * 100.0


def _quarter_label(text):
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s*,?\s+(\d{4})", text)
    if not m:
        return None
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    month = months.index(m.group(1)) + 1
    year = int(m.group(2))
    if 4 <= month <= 6:
        q, fy = "Q1", year + 1
    elif 7 <= month <= 9:
        q, fy = "Q2", year + 1
    elif 10 <= month <= 12:
        q, fy = "Q3", year + 1
    else:
        q, fy = "Q4", year
    return f"{q} FY{fy % 100:02d}"


_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4,
           "may": 5, "june": 6, "july": 7, "august": 8, "september": 9,
           "october": 10, "november": 11, "december": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_RX = ("January|February|March|April|May|June|July|August|"
             "September|October|November|December|"
             "Jan\\.?|Feb\\.?|Mar\\.?|Apr\\.?|Jun\\.?|Jul\\.?|Aug\\.?|"
             "Sep\\.?|Sept\\.?|Oct\\.?|Nov\\.?|Dec\\.?")


def _quarter_label_from_header(lines, rev_idx):
    """Quarter label from the chosen table's own header block.

    The header block (heading + column headers above the revenue
    row) lists several dates: current quarter, preceding quarter,
    year-ago quarter, half-year ends, year end. The LATEST
    quarter-end month in that block is the current quarter, no
    matter what order extraction emitted the columns in (IGL's
    layout order puts June 2026 before September 2026, which is
    what broke the old whole-document first-match approach).
    Returns None when the block holds no usable date.
    """
    block = "\n".join(lines[max(0, rev_idx - 80):rev_idx])
    dates = []
    for rx in (rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RX})\s*,?\s+(\d{{4}})",
               rf"({_MONTH_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(\d{{4}})",
               rf"({_MONTH_RX})\s*,?\s+(\d{{4}})"):
        for m in re.finditer(rx, block, re.IGNORECASE):
            groups = m.groups()
            if len(groups) == 3 and groups[0].isdigit():
                month = _MONTHS[groups[1].lower().rstrip(".")]
                year = int(groups[2])
            elif len(groups) == 3:
                month = _MONTHS[groups[0].lower().rstrip(".")]
                year = int(groups[2])
            else:
                month = _MONTHS[groups[0].lower().rstrip(".")]
                year = int(groups[1])
            dates.append((year, month))
    if not dates:
        return None
    qends = [(y, mo) for (y, mo) in dates if mo in (3, 6, 9, 12)]
    year, month = max(qends) if qends else max(dates)
    if 4 <= month <= 6:
        q, fy = "Q1", year + 1
    elif 7 <= month <= 9:
        q, fy = "Q2", year + 1
    elif 10 <= month <= 12:
        q, fy = "Q3", year + 1
    else:
        q, fy = "Q4", year
    return f"{q} FY{fy % 100:02d}"


def _parse_segment(text):
    """Parse one results filing's text. Returns dict or None (refused)."""
    lines = text.splitlines()
    # The results table usually sits AFTER a covering letter, so the
    # revenue row can be hundreds of lines in (CHEMBOND: line 175,
    # RESPONIND: line 446) -- searching only the first 60 lines
    # missed it entirely. Scan the whole document and take the first
    # revenue row that actually carries at least 3 values, so prose
    # mentions of revenue without a table are skipped.
    rev_idx = None
    for pattern in (r"revenue\s+from\s+operations|income\s+from\s+operations",
                    r"from\s+operations"):
        # Second pass is for labels pypdf mangled beyond recognition
        # (IGL consolidated: "RcYcnuc from operations"); the >=3
        # values guard keeps prose mentions out either way.
        for i, ln in enumerate(lines):
            if re.search(pattern, ln, re.IGNORECASE):
                if len(_row_values(ln)) >= 3:
                    rev_idx = i
                    break
        if rev_idx is not None:
            break
    if rev_idx is None:
        return _refuse("no-revenue-row")
    rev_pos = sum(len(l) + 1 for l in lines[:rev_idx])
    factor = _find_unit_factor(text, rev_pos)
    if factor == 1.0:
        # pypdf sometimes orders a table's unit title after the
        # table body, outside the window above. Fall back to the
        # document's unit title line ("(Rs. in Lakhs ...)") when
        # one exists; prose mentions do not match that pattern.
        m = re.search(r"rs\.?\s+in\s+(lakhs?|lacs|millions?)", text,
                      re.IGNORECASE)
        if m:
            factor = 0.01 if ("lakh" in m.group(1).lower()
                              or "lac" in m.group(1).lower()) else 0.1

    rev = _row_values(lines[rev_idx])
    if len(rev) < 3:
        return _refuse("revenue-row-short")
    sales, sales_prev, sales_ly = rev[0] * factor, rev[1] * factor, rev[2] * factor

    oi_idx = _first_match_line(lines, rev_idx + 1, r"other income")
    if oi_idx is None:
        oi_idx = _first_match_line_sq(lines, rev_idx + 1, ["otherincome"])
    other = None
    if oi_idx is not None:
        vals = _row_values(lines[oi_idx])
        if len(vals) >= 3:
            other = (vals[0] * factor, vals[1] * factor, vals[2] * factor)

    ti_idx = _first_match_line(lines, rev_idx + 1, r"total income")
    if ti_idx is None:
        ti_idx = _first_match_line_sq(lines, rev_idx + 1, ["totalincome"])
    total = None
    if ti_idx is not None:
        vals = _row_values(lines[ti_idx])
        if len(vals) >= 3:
            total = (vals[0] * factor, vals[1] * factor, vals[2] * factor)

    if other is None and total is not None:
        other = (total[0] - sales, total[1] - sales_prev, total[2] - sales_ly)
    if total is None and other is not None:
        total = (sales + other[0], sales_prev + other[1], sales_ly + other[2])
    if other is None or total is None:
        return _refuse("no-other-no-total-row")
    # Cross-check: Total Income must equal Revenue + Other Income.
    if abs(total[0] - (sales + other[0])) > max(MONEY_TOL, 0.005 * total[0]):
        return _refuse("cross-check-mismatch")

    # PAT label varies by filing: "Profit/(Loss) For the Period",
    # "Net Profit/(Loss) after tax", "Profit after tax". Try the
    # most specific first; exclusions keep "before tax" and
    # comprehensive-income rows out. Continuing-operations rows
    # are subtotals, not the period total: prefer a plain total
    # row first (EXXARO's continuing row is mangled and fails the
    # half-year guard while its total row parses clean), but fall
    # back to the continuing row when it is the only PAT row the
    # filing offers (MALU OCR has no separate total row).
    def _find_pat(exclude_continuing):
        excl = ["before tax", "comprehensive", "attributable",
                "non-controlling", "discontinu"]
        excl_sq = ["beforetax", "comprehensive", "attributable",
                   "noncontrolling", "discontinu"]
        if exclude_continuing:
            excl = excl + ["continuing"]
            excl_sq = excl_sq + ["continuing"]
        for pat_pat in (r"(profit|loss).*for the period",
                        r"net profit.*after tax",
                        r"(profit|loss).*after tax",
                        r"net profit"):
            idx = _first_match_line(lines, rev_idx + 1, pat_pat,
                                    exclude=excl)
            if idx is not None:
                return idx
        # Whitespace-mangled labels ("Profit for th e period").
        for needles in (["profit", "fortheperiod"],
                        ["loss", "fortheperiod"],
                        ["netprofit", "aftertax"],
                        ["profit", "aftertax"],
                        ["netprofit"]):
            idx = _first_match_line_sq(lines, rev_idx + 1, needles,
                                       exclude=excl_sq)
            if idx is not None:
                return idx
        return None

    pat_idx = _find_pat(True)
    if pat_idx is None:
        pat_idx = _find_pat(False)
    if pat_idx is None:
        return _refuse("no-pat-row")
    pat = _row_values(lines[pat_idx])
    if len(pat) < 3 and pat_idx + 1 < len(lines):
        # Label wrapped: values sit on the following line (MALU OCR).
        pat = _row_values(lines[pat_idx + 1])
    if len(pat) < 3:
        return _refuse("pat-row-short")
    pat_c, pat_p, pat_l = pat[0] * factor, pat[1] * factor, pat[2] * factor
    # Magnitude guard: mangled text can pass the cross-check with
    # absurd figures (SJVN consolidated once read Sales as
    # 1,032,403,638 Cr). No real quarterly figure is anywhere near
    # MONEY_MAX, so refuse the whole parse and let the caller fall
    # back (standalone) or post text only.
    for v in (sales, sales_prev, sales_ly, other[0], other[1], other[2],
              total[0], total[1], total[2], pat_c, pat_p, pat_l):
        if abs(v) > MONEY_MAX:
            return _refuse("magnitude-guard")
    # Sign comes from the value itself (parentheses or a leading
    # minus in the value chunk). Do NOT scan the whole line for a
    # minus: labels like "(9-10)" are row arithmetic, not a loss
    # sign, and that false negative flipped CHEMBOND's PAT to -4.30
    # when the filing shows a 430.06 profit.

    eps = None
    eps_hdr = _first_match_line(lines, rev_idx + 1,
                                r"earning[s]? per (equity )?share")
    if eps_hdr is not None:
        e_idx = _first_match_line(lines, eps_hdr + 1, r"basic")
        if e_idx is not None:
            vals = _row_values(lines[e_idx])
            if len(vals) >= 3:
                eps = (vals[0], vals[1], vals[2])
        if eps is None:
            # Some filings (IGL) put the EPS values on the header
            # line itself ("Earnings per share ... 0.15* 1.33*
            # 2.66*") and leave the Basic line valueless.
            hdr_vals = _row_values(lines[eps_hdr].replace("*", ""))
            if len(hdr_vals) >= 3:
                eps = (hdr_vals[0], hdr_vals[1], hdr_vals[2])

    # EPS<->PAT sign check: EPS = PAT / shares, so their signs must
    # agree for the same period. A mismatch means the EPS row came
    # from a different basis or row than the PAT row (basis-mixing),
    # so refuse rather than show a contradictory card.
    if (eps is not None and eps[0] is not None and pat_c != 0
            and eps[0] != 0 and (eps[0] > 0) != (pat_c > 0)):
        return _refuse("eps-pat-sign-mismatch")

    # EPS<->PAT ratio guard: the share count barely moves quarter
    # to quarter, so implied shares (PAT / EPS) must agree across
    # the three periods. OCR can mangle one EPS value beyond
    # recognition while the sign check still passes (FINPIPE OCR
    # read 1852.0 for a true 1.86, with the other two periods
    # landing on the half-year column's 3.71/3.59). When the implied
    # share counts spread more than 2x, the EPS row is
    # untrustworthy: blank the EPS (BALKRISHNA precedent) rather
    # than show a wrong figure or refuse a card whose money numbers
    # all verify exactly. Skipped when any PAT is near zero (ratios
    # unstable) or any EPS is zero.
    if (eps is not None
            and all(p != 0 and abs(p) >= 1.0
                    for p in (pat_c, pat_p, pat_l))
            and all(e is not None and e != 0 for e in eps)):
        shares = [abs(p / e) for p, e in
                  zip((pat_c, pat_p, pat_l), eps)]
        if max(shares) > 2.0 * min(shares):
            eps = None

    sales_yoy = _pct(sales, sales_ly)
    sales_qoq = _pct(sales, sales_prev)
    pat_yoy = _pct_signed(pat_c, pat_l)
    pat_qoq = _pct_signed(pat_c, pat_p)
    rating, reason = verdict(sales_yoy, pat_yoy)

    def margin(p, s):
        return (p / s * 100.0) if s else None

    m_c, m_p, m_l = margin(pat_c, sales), margin(pat_p, sales_prev), margin(pat_l, sales_ly)
    # Half-year guard (Q2 filings only, standard column order
    # [Q_cur, Q_prev, Q_ly, HY_cur, ...]): Q_cur + Q_prev must equal
    # the HY column. Layout-mode extraction can shred a two-block
    # table into plausible-looking rows (RESPONIND: sales 204.89 /
    # 1.93 / 0.16 with a 51% HY mismatch) that pass the Total cross-
    # check because Other is derived; the HY sum catches those.
    # Restated comparatives can make a true HY differ slightly, so
    # the tolerance mirrors the Total cross-check (0.5% relative).
    # Rows without a 4th value simply skip the check.
    ql = (_quarter_label_from_header(lines, rev_idx)
          or _quarter_label(text))
    if ql is not None and ql.startswith("Q2"):
        if len(rev) >= 4:
            sales_hy = rev[3] * factor
            if abs((sales + sales_prev) - sales_hy) > max(
                    MONEY_TOL, 0.005 * abs(sales_hy)):
                return _refuse("hy-sales-mismatch")
        if len(pat) >= 4:
            pat_hy = pat[3] * factor
            if abs((pat_c + pat_p) - pat_hy) > max(
                    MONEY_TOL, 0.005 * abs(pat_hy)):
                return _refuse("hy-pat-mismatch")

    return {
        "sales": (sales, sales_prev, sales_ly),
        "other": other,
        "total": total,
        "pat": (pat_c, pat_p, pat_l),
        "eps": eps,
        "sales_qoq": sales_qoq, "sales_yoy": sales_yoy,
        "pat_qoq": pat_qoq, "pat_yoy": pat_yoy,
        "pat_margin": (m_c, m_p, m_l),
        "total_qoq": _pct(total[0], total[1]),
        "total_yoy": _pct(total[0], total[2]),
        "eps_qoq": _pct(eps[0], eps[1]) if eps else None,
        "eps_yoy": _pct(eps[0], eps[2]) if eps else None,
        "rating": rating, "reason": reason,
        "quarter_label": (_quarter_label_from_header(lines, rev_idx)
                          or _quarter_label(text)),
    }


def _consolidated_heading(lines):
    """Index of the consolidated results statement heading, or None.

    Requires all three words on one line ("Statement of
    Consolidated ... Financial Results") starting with "Statement",
    so covering-letter mentions and auditor reports ("We have
    reviewed the accompanying statement of ... consolidated ...")
    do not match. Lines mentioning "standalone" are skipped: a
    combined cover line ("Statements of ... Financial Results
    (Standalone and Consolidated)") is not the consolidated table's
    own heading, and starting the segment there would parse the
    standalone table first and mislabel it Consolidated.
    """
    for i, ln in enumerate(lines):
        low = ln.strip().lower()
        if (low.startswith("statement") and "consolidated" in low
                and "result" in low and "standalone" not in low):
            return i
    return None


def parse_results(text, layout_text=None):
    """Parse one filing, consolidated-first with standalone fallback.

    Tries the consolidated segment in the default extraction first;
    if that fails and layout-mode text is supplied, retries the
    consolidated segment there (layout mode keeps table columns on
    one line, which recovers column-block filings such as LICHSGFIN
    and GABRIEL whose default extraction scatters the table).
    Only then falls back to the standalone table. Returns the parsed
    dict with "basis" set to "Consolidated" or "Standalone", or None
    when no table parses (watcher then posts "Results Filed" text
    only). LAST_REFUSAL holds the last segment's refusal reason.
    """
    global LAST_REFUSAL
    LAST_REFUSAL = None
    lines = text.splitlines()
    ci = _consolidated_heading(lines)
    if ci is not None:
        parsed = _parse_segment("\n".join(lines[ci:]))
        if parsed is not None:
            parsed["basis"] = "Consolidated"
            return parsed
    if layout_text is not None:
        llines = layout_text.splitlines()
        li = _consolidated_heading(llines)
        if li is not None:
            parsed = _parse_segment("\n".join(llines[li:]))
            if parsed is not None:
                parsed["basis"] = "Consolidated"
                return parsed
    parsed = _parse_segment(text)
    if parsed is not None:
        parsed["basis"] = "Standalone"
    return parsed
