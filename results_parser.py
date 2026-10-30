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


def _row_values(line):
    """Extract the value section of a table row as floats.

    Split fragments are joined first, on TOKEN boundaries only:
    a bare token joins the following decimal token only when it
    contains a comma ("22,4" + "17.29") or is at most 2 digits
    ("2" + "1,759.40"). A 3-digit token like "063" is a complete
    mangled value (0.63), not a fragment, so it is never joined.
    The value section then starts at the first decimal token.
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
        r"October|November|December)\s+(\d{4})", text)
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
    rev_idx = _first_match_line(lines, 0, r"revenue from operations")
    if rev_idx is None:
        rev_idx = _first_match_line(lines, 0, r"income from operations")
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

    pat_idx = _first_match_line(
        lines, rev_idx + 1, r"(profit|loss).*for the period",
        exclude=["before tax", "comprehensive", "attributable",
                 "non-controlling", "discontinu"])
    if pat_idx is None:
        return None
    pat = _row_values(lines[pat_idx])
    if len(pat) < 3:
        return None
    pat_c, pat_p, pat_l = pat[0] * factor, pat[1] * factor, pat[2] * factor
    # A leading minus in the raw row marks a loss.
    if re.search(r"-\s*\d", lines[pat_idx]):
        pat_c = -abs(pat_c)

    eps = None
    eps_hdr = _first_match_line(lines, rev_idx + 1,
                                r"earnings per equity share")
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
