"""Parse Indian quarterly results PDFs into structured numbers.

Proven lessons baked in (all from real NSE filings):
- The standalone table precedes the consolidated table, so the FIRST
  results table found is used (standalone-first).
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


def _repair_numeric_tokens(tokens):
    """Repair mangled numeric tokens inside a table row's value section."""
    out = []
    for tok in tokens:
        neg = tok.startswith("-")
        t = tok.lstrip("-").replace(",", "")
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
        if (i + 1 < len(toks)
                and re.fullmatch(r"-?\d[\d,]*", tok)
                and "." not in tok
                and re.fullmatch(r"-?\d[\d,]*\.\d+", toks[i + 1])
                and ("," in tok or (len(tok.lstrip("-")) <= 2
                                    and not tok.lstrip("-").startswith("0")))):
            joined.append(tok + toks[i + 1])
            i += 2
        else:
            joined.append(tok)
            i += 1
    start = None
    for i, tok in enumerate(joined):
        if re.fullmatch(r"-?\d[\d,]*\.\d+", tok):
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
        for chunk in parts[1:]:
            v = _parse_chunk(chunk)
            if v is not None:
                vals.append(v)
        if len(vals) >= 3:
            return vals
    return _row_values_tokens(line)


def _find_unit_factor(text, revenue_pos):
    """Units from the header above the revenue row."""
    head = text[max(0, revenue_pos - 3000):revenue_pos].lower()
    if "lakh" in head:
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


def parse_results(text):
    """Parse one results filing's text. Returns dict or None (refused)."""
    lines = text.splitlines()
    # The results table usually sits AFTER a covering letter, so the
    # revenue row can be hundreds of lines in (CHEMBOND: line 175,
    # RESPONIND: line 446) -- searching only the first 60 lines
    # missed it entirely. Scan the whole document and take the first
    # revenue row that actually carries at least 3 values, so prose
    # mentions of revenue without a table are skipped.
    rev_idx = None
    for i, ln in enumerate(lines):
        if re.search(r"revenue from operations|income from operations",
                     ln, re.IGNORECASE):
            if len(_row_values(ln)) >= 3:
                rev_idx = i
                break
    if rev_idx is None:
        return None
    rev_pos = sum(len(l) + 1 for l in lines[:rev_idx])
    factor = _find_unit_factor(text, rev_pos)

    rev = _row_values(lines[rev_idx])
    if len(rev) < 3:
        return None
    sales, sales_prev, sales_ly = rev[0] * factor, rev[1] * factor, rev[2] * factor

    oi_idx = _first_match_line(lines, rev_idx + 1, r"other income")
    other = None
    if oi_idx is not None:
        vals = _row_values(lines[oi_idx])
        if len(vals) >= 3:
            other = (vals[0] * factor, vals[1] * factor, vals[2] * factor)

    ti_idx = _first_match_line(lines, rev_idx + 1, r"total income")
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
        return None
    # Cross-check: Total Income must equal Revenue + Other Income.
    if abs(total[0] - (sales + other[0])) > max(MONEY_TOL, 0.005 * total[0]):
        return None

    # PAT label varies by filing: "Profit/(Loss) For the Period",
    # "Net Profit/(Loss) after tax", "Profit after tax". Try the
    # most specific first; exclusions keep "before tax" and
    # comprehensive-income rows out.
    pat_idx = None
    for pat_pat in (r"(profit|loss).*for the period",
                    r"net profit.*after tax",
                    r"(profit|loss).*after tax",
                    r"net profit"):
        pat_idx = _first_match_line(
            lines, rev_idx + 1, pat_pat,
            exclude=["before tax", "comprehensive", "attributable",
                     "non-controlling", "discontinu"])
        if pat_idx is not None:
            break
    if pat_idx is None:
        return None
    pat = _row_values(lines[pat_idx])
    if len(pat) < 3:
        return None
    pat_c, pat_p, pat_l = pat[0] * factor, pat[1] * factor, pat[2] * factor
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

    sales_yoy = _pct(sales, sales_ly)
    sales_qoq = _pct(sales, sales_prev)
    pat_yoy = _pct_signed(pat_c, pat_l)
    pat_qoq = _pct_signed(pat_c, pat_p)
    rating, reason = verdict(sales_yoy, pat_yoy)

    def margin(p, s):
        return (p / s * 100.0) if s else None

    m_c, m_p, m_l = margin(pat_c, sales), margin(pat_p, sales_prev), margin(pat_l, sales_ly)
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
        "quarter_label": _quarter_label(text),
    }
