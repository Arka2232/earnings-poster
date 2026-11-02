"""XBRL parsing for NSE Integrated Filing results (Phase 1: recovery).

Wired into watcher.py's XBRL retry (maybe_xbrl_retry): when a filing
posts as text/screenshot because the PDF chain failed, later runs
and the nightly pass retry it from the filing's own XBRL document.
This is RECOVERY, not a replacement for PDF-first posting: XBRL
lags the PDF by 0.1-6.2h, and one XBRL document holds the current
quarter + half-year only (no previous quarter, no year-ago), so
comparatives come from the half-year subtraction (Q2), the
prior-year document, or — when a PDF parse is available — the
PDF's own printed columns (see the iron disagreement rule in
watcher.py).

Source (verified live, 2026-11-02):
  GET https://www.nseindia.com/api/integrated-filing-results
      ?index=equities&symbol=<SYM>[&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY]
  -> rows with type "Integrated Filing- Financials", qe_Date
     ("30-SEP-2026"), consolidated ("Consolidated"/"Standalone"),
     type_Sub ("Original"/"Revision") and a direct "xbrl" URL
     (nsearchives .../corporate/xbrl/INTEGRATED_FILING_*.xml).
     from_date/to_date are DD-MM-YYYY and filter the broadcast
     window (verified: a 60-day window returns 3-4 rows even for
     RELIANCE/HDFCBANK, so windowed queries beat the latest-20
     cap). Revision rows carry broadcast_Date None but still
     appear inside their creation window.
  Since 2025-04-01 (NSE circular NSE/CML/2025/20) listed entities
  must file financial results in this XBRL utility on the same day
  as the PDF, so the XBRL is the company's own tagged filing, not
  a third-party restatement.

What the XBRL contains (Ind-AS taxonomy, SEBI 2026-01-31):
  - One duration context per period, DIMENSIONLESS: the current
    quarter (e.g. 2026-07-01 -> 2026-09-30) and the cumulative
    half-year (2026-04-01 -> 2026-09-30). Contexts are selected
    by PERIOD DATES, never by context id ("OneD"/"FourD" happen
    to be stable but are not trusted).
  - NO previous-quarter and NO year-ago-quarter durations in the
    filing itself. That is the key structural finding: one XBRL
    document cannot build the full card alone. Previous quarter
    for a Q2 filing is exactly HY - Q (verified: EXXARO
    197.3623 - 112.1399 = 85.2224 = PDF Q1; FINPIPE exact too);
    year-ago comes from the PRIOR-YEAR filing's XBRL (same basis,
    same quarter-end month), fetched via the same API rows
    (qe_Date "30-SEP-2025"). For Q1/Q3/Q4 filings the previous
    quarter likewise needs that quarter's own XBRL document.
  - Money facts are raw INR (unit INR); EPS facts are INRPerShare
    and are NEVER scaled. Money is converted /1e7 -> Rs Cr.
  - Concept mapping (Ind-AS only): RevenueFromOperations,
    OtherIncome, Income (Total), ProfitLossForPeriod (PAT, total
    incl. non-controlling interest — the same row the PDF parser
    picks, since it excludes "attributable" rows),
    BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations.
  - BANKING and other taxonomies (e.g. HDFCBANK's
    INTEGRATED_FILING_BANKING file: InterestEarned,
    InterestExpended, OperatingProfitBeforeProvisionAndContingencies,
    ProfitLossForThePeriod) use different concepts and are
    REFUSED by this module (returns None) rather than mapped
    by guesswork. A banking mapping is separate follow-up work.

Guards (same iron rule as the PDF path — never invent):
  - Dimensioned facts (segment/typedMember contexts) are ignored;
    only dimensionless duration facts are used.
  - A concept with zero or 2+ facts in the chosen context refuses
    the parse (ambiguity is never resolved by picking one).
  - Total cross-check on XBRL facts: |total - (sales + other)|
    <= 0.02 Cr (XBRL values are exact to ~0.001 Cr, so this is
    tight; a mismatch refuses, exactly like the PDF path).
  - Magnitude guard (same MONEY_MAX as results_parser).
  - EPS sign check (all periods) and EPS ratio guard (blank EPS
    on >2x implied-share spread), mirroring results_parser.
  - Rating/reason reuse results_parser.verdict; the returned dict
    is shaped like a results_parser result so card_data_from_parse
    works unchanged. Extra key "source" = "XBRL".

Row selection (pure function over API rows):
  select_xbrl_row(rows, qe_date, basis) prefers type_Sub
  "Original" over "Revision" for the same quarter+basis. Rationale:
  the two Revision files sampled (EXXARO/NITINSPIN standalone)
  were byte-identical to their Originals, so Original is the
  safe default; a content-differing Revision is handled by the
  retry layer (watcher.resolve_xbrl_row), which compares
  downloads and REFUSES on a difference instead of assuming.
"""

import re
import xml.etree.ElementTree as ET

from results_parser import MONEY_MAX, verdict

XBRL_NS = "{http://www.xbrl.org/2003/instance}"

MONEY_CONCEPTS = ("RevenueFromOperations", "OtherIncome", "Income",
                  "ProfitLossForPeriod")
EPS_CONCEPT = ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
LAST_REFUSAL = None

_XBRL_TOL = 0.02  # Rs Cr; XBRL facts are exact, so this is tight


def _refuse(reason):
    global LAST_REFUSAL
    LAST_REFUSAL = reason
    return None


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _contexts(root):
    """id -> (start, end, has_dimensions) for duration contexts."""
    out = {}
    for c in root.findall(f"{XBRL_NS}context"):
        per = c.find(f"{XBRL_NS}period")
        scen = c.find(f"{XBRL_NS}scenario")
        dim = scen is not None and len(list(scen)) > 0
        start = end = None
        if per is not None:
            for ch in per:
                t = _local(ch.tag)
                if t == "startDate":
                    start = ch.text
                elif t == "endDate":
                    end = ch.text
        if start and end:
            out[c.get("id")] = (start, end, dim)
    return out


def _facts(root):
    """(concept, contextRef) -> [elements]; XBRL instance facts only."""
    out = {}
    for el in root:
        if el.tag.startswith(XBRL_NS):
            continue
        out.setdefault((_local(el.tag), el.get("contextRef")), []).append(el)
    return out


def _value(facts, concept, ctx, scale):
    """One unambiguous fact -> scaled float, else None.

    A missing fact, a duplicated fact, a non-numeric value, or a
    non-INR money unit all yield None (the caller refuses); the
    sign attribute ("-") negates, per the XBRL spec.
    """
    els = facts.get((concept, ctx), [])
    if len(els) != 1:
        return None
    el = els[0]
    if scale is not None and el.get("unitRef") != "INR":
        return None
    try:
        v = float((el.text or "").strip())
    except (ValueError, AttributeError):
        return None
    if el.get("sign") == "-":
        v = -v
    return v * scale if scale is not None else v


def _quarter_label(end_date):
    """'2026-09-30' -> 'Q2 FY27' (same mapping as results_parser)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", end_date or "")
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if 4 <= month <= 6:
        q, fy = "Q1", year + 1
    elif 7 <= month <= 9:
        q, fy = "Q2", year + 1
    elif 10 <= month <= 12:
        q, fy = "Q3", year + 1
    else:
        q, fy = "Q4", year
    return f"{q} FY{fy % 100:02d}"


def _period_values(facts, ctx):
    """(sales, other, total, pat, eps) in Cr (eps unscaled), or None."""
    sales = _value(facts, "RevenueFromOperations", ctx, 1e-7)
    other = _value(facts, "OtherIncome", ctx, 1e-7)
    total = _value(facts, "Income", ctx, 1e-7)
    pat = _value(facts, "ProfitLossForPeriod", ctx, 1e-7)
    eps = _value(facts, EPS_CONCEPT, ctx, None)
    if sales is None or other is None or total is None or pat is None:
        return None
    return (sales, other, total, pat, eps)


def extract_periods(xml_bytes, qe_end):
    """Current-quarter + half-year period values for one XBRL doc.

    Returns {"quarter": (...), "half_year": (...)|None,
    "quarter_label": str} or None when the doc is not a usable
    Ind-AS results filing for that quarter-end (wrong taxonomy
    concepts, missing contexts/facts, dimensioned-only data).
    "half_year" is the longest cumulative duration in the doc
    (half-year for Q2 filings, full-year for Q4 filings); only
    the Q2 use (HY - Q = previous quarter) is relied on.
    Contexts are chosen by dates: current quarter = the
    dimensionless duration ending on qe_end with the LATEST start
    (shortest period); half-year = the one with the EARLIEST start.
    """
    global LAST_REFUSAL
    LAST_REFUSAL = None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return _refuse("xml-parse-error")
    ctx = _contexts(root)
    durs = sorted(
        [(cid, s, e) for cid, (s, e, dim) in ctx.items()
         if not dim and e == qe_end],
        key=lambda t: t[1], reverse=True)
    if not durs:
        return _refuse("no-quarter-context")
    facts = _facts(root)
    quarter = _period_values(facts, durs[0][0])
    if quarter is None:
        return _refuse("missing-quarter-fact")
    half_year = None
    if len(durs) > 1:
        half_year = _period_values(facts, durs[-1][0])
    return {"quarter": quarter, "half_year": half_year,
            "quarter_label": _quarter_label(qe_end)}


def _pct(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return (cur / base - 1.0) * 100.0


def _pct_signed(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return (cur - base) / abs(base) * 100.0


def build_card(current_xml, qe_end, basis,
               prior_year_xml=None, prev_quarter_xml=None):
    """Build a parse_results-shaped dict from XBRL document(s).

    current_xml: this quarter's filing (same basis as the card).
    prior_year_xml: same quarter last year's filing (same basis),
      for YoY; None -> YoY fields are None (card shows '-').
    prev_quarter_xml: previous quarter's filing (same basis), for
      QoQ when the quarter is not Q2; for Q2, prev = HY - Q from
      the current document itself (exact, verified) and this
      argument is ignored.
    Returns None (refused) on any guard failure — never a
    half-guessed card.
    """
    global LAST_REFUSAL
    LAST_REFUSAL = None
    cur = extract_periods(current_xml, qe_end)
    if cur is None:
        return None
    (sales, other, total, pat, eps) = cur["quarter"]
    if abs(total - (sales + other)) > _XBRL_TOL:
        return _refuse("xbrl-cross-check-mismatch")
    for v in (sales, other, total, pat):
        if abs(v) > MONEY_MAX:
            return _refuse("magnitude-guard")
    ql = cur["quarter_label"]
    sales_prev = other_prev = total_prev = pat_prev = eps_prev = None
    if ql and ql.startswith("Q2") and cur["half_year"] is not None:
        # HY - Q is the previous quarter, exactly (verified for
        # sales and PAT; other/total likewise subtract). EPS does
        # NOT subtract (a ratio, not a flow), so Q2 EPS QoQ stays
        # None unless a prev-quarter document is supplied.
        (hy_s, hy_o, hy_t, hy_p, _hy_e) = cur["half_year"]
        sales_prev = hy_s - sales
        other_prev = hy_o - other
        total_prev = hy_t - total
        pat_prev = hy_p - pat
    elif prev_quarter_xml is not None:
        prev = extract_periods(prev_quarter_xml, _prev_qe_end(qe_end))
        if prev is not None:
            sales_prev = prev["quarter"][0]
            other_prev = prev["quarter"][1]
            total_prev = prev["quarter"][2]
            pat_prev = prev["quarter"][3]
            eps_prev = prev["quarter"][4]
    sales_ly = other_ly = total_ly = pat_ly = eps_ly = None
    if prior_year_xml is not None:
        py = extract_periods(prior_year_xml, _prev_year_end(qe_end))
        if py is not None:
            sales_ly = py["quarter"][0]
            other_ly = py["quarter"][1]
            total_ly = py["quarter"][2]
            pat_ly = py["quarter"][3]
            eps_ly = py["quarter"][4]
    if eps is not None:
        for e, p in ((eps, pat), (eps_prev, pat_prev), (eps_ly, pat_ly)):
            if (e is not None and e != 0 and p is not None and p != 0
                    and (e > 0) != (p > 0)):
                return _refuse("eps-pat-sign-mismatch")
        if (eps_prev is not None and eps_ly is not None
                and all(p is not None and abs(p) >= 1.0
                        for p in (pat, pat_prev, pat_ly))
                and all(e is not None and e != 0
                        for e in (eps, eps_prev, eps_ly))):
            shares = [abs(p / e) for p, e in
                      zip((pat, pat_prev, pat_ly), (eps, eps_prev, eps_ly))]
            if max(shares) > 2.0 * min(shares):
                eps = eps_prev = eps_ly = None
    eps_t = None if eps is None else (eps, eps_prev, eps_ly)
    sales_yoy = _pct(sales, sales_ly)
    pat_yoy = _pct_signed(pat, pat_ly)
    rating, reason = verdict(sales_yoy, pat_yoy)

    def margin(p, s):
        return (p / s * 100.0) if s else None

    return {
        "sales": (sales, sales_prev, sales_ly),
        "other": (other, other_prev, other_ly),
        "total": (total, total_prev, total_ly),
        "pat": (pat, pat_prev, pat_ly),
        "eps": eps_t,
        "sales_qoq": _pct(sales, sales_prev),
        "sales_yoy": sales_yoy,
        "pat_qoq": _pct_signed(pat, pat_prev),
        "pat_yoy": pat_yoy,
        "pat_margin": (margin(pat, sales),
                       margin(pat_prev, sales_prev) if sales_prev else None,
                       margin(pat_ly, sales_ly) if sales_ly else None),
        "total_qoq": _pct(total, total_prev),
        "total_yoy": _pct(total, total_ly),
        "eps_qoq": _pct(eps, eps_prev) if eps_t else None,
        "eps_yoy": _pct(eps, eps_ly) if eps_t else None,
        "rating": rating, "reason": reason,
        "quarter_label": ql, "basis": basis, "source": "XBRL",
    }


def _prev_qe_end(qe_end):
    """Quarter-end date of the previous quarter (YYYY-MM-DD)."""
    y, m, _d = (int(x) for x in qe_end.split("-"))
    prev = {3: (y - 1, 12), 6: (y, 3), 9: (y, 6), 12: (y, 9)}.get(m)
    if prev is None:
        return None
    py, pm = prev
    last_day = {3: 31, 6: 30, 9: 30, 12: 31}[pm]
    return f"{py:04d}-{pm:02d}-{last_day:02d}"


def _prev_year_end(qe_end):
    """Same quarter-end one year earlier."""
    y, m, d = (int(x) for x in qe_end.split("-"))
    return f"{y - 1:04d}-{m:02d}-{d:02d}"


def select_xbrl_row(rows, qe_date, basis):
    """Pick one integrated-filing row for (quarter-end, basis).

    rows: integrated-filing-results API rows. Filters to
    type "Integrated Filing- Financials", matching qe_Date and
    consolidated basis, with a non-empty xbrl URL. Prefers
    type_Sub "Original" over "Revision" (sampled Revisions were
    byte-identical to their Originals). Returns the row or None.
    qe_date format matches the API: "30-SEP-2026".
    """
    cands = [r for r in rows
             if r.get("type") == "Integrated Filing- Financials"
             and r.get("qe_Date") == qe_date
             and r.get("consolidated") == basis
             and r.get("xbrl")]
    if not cands:
        return None
    cands.sort(key=lambda r: (0 if r.get("type_Sub") == "Original" else 1,
                              r.get("broadcast_Date") or "",
                              r.get("creation_Date") or ""))
    return cands[0]


def fetch_integrated_rows(symbol, timeout=30, attempts=3,
                          from_date=None, to_date=None, index="equities"):
    """GET integrated-filing-results rows for one symbol (NSE API).

    from_date/to_date are DD-MM-YYYY broadcast-window filters
    (verified live 2026-11-02); a window around the filing date
    beats the latest-20 cap. Same header/retry posture as
    watcher.fetch_symbol_feed. Raises on persistent failure
    (caller treats as XBRL-unavailable, not as data). Kept
    separate from parsing so tests never touch the network.
    """
    import time as _time
    import requests
    url = "https://www.nseindia.com/api/integrated-filing-results"
    params = {"index": index, "symbol": symbol}
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    }
    last = None
    for _ in range(attempts):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data.get("data") or []
            return data
        except Exception as exc:
            last = exc
            _time.sleep(2)
    raise RuntimeError(f"integrated-filing fetch failed ({symbol}): {last}")


def download_xbrl(url, timeout=60, attempts=3):
    """Download one XBRL document's bytes (nsearchives direct URL).

    Same header/retry posture as the integrated-rows fetch.
    Raises on persistent failure; an empty body is a failure,
    not a document (a truncated XML must never be parsed).
    """
    import time as _time
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com/",
    }
    last = None
    for _ in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            if not r.content:
                raise RuntimeError("empty XBRL download")
            return r.content
        except Exception as exc:
            last = exc
            _time.sleep(2)
    raise RuntimeError(f"XBRL download failed ({url}): {last}")


BANKING_CONCEPTS = ("InterestEarned", "InterestExpended",
                    "OperatingProfitBeforeProvisionAndContingencies",
                    "ProfitLossForThePeriod")


def detect_taxonomy(xml_bytes):
    """"ind-as" | "banking" | "other" | None for one XBRL document.

    Ind-AS is identified by RevenueFromOperations (the concept
    extract_periods keys on); banking by its interest-earned /
    interest-expended concepts with no Ind-AS revenue concept.
    Anything else parseable is "other" (a taxonomy this code
    does not map — refused, never guessed). None means the bytes
    are not parseable XML at all (a corrupt download, retried
    later rather than blocked).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    concepts = {_local(el.tag) for el in root
                if not el.tag.startswith(XBRL_NS)}
    if "RevenueFromOperations" in concepts:
        return "ind-as"
    if concepts & set(BANKING_CONCEPTS):
        return "banking"
    return "other"
