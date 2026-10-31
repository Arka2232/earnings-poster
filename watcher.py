"""NSE results watcher -> image card -> Telegram post.

Sources: NSE's own public corporate-announcements feed (free, no key).
Each new "Outcome of Board Meeting" filing whose text mentions
"financial results" is downloaded from NSE's archive, parsed, and
posted once. State (seen filings) lives in state.json, committed back
to the repo by the workflow so runners stay in sync.

Modes:
  --check   list new results filings, post nothing, write nothing
  --seed    mark all current results filings seen, post nothing
  (default) post new results filings (image card + buttons)

  BACKFILL_SYMBOLS (env, comma-separated) — before the normal feed
  check, sweep each symbol's own announcement history for today's
  results filings the latest-20 feed may have missed (e.g. after a
  scheduler outage). Same filter, same pipeline, same seen-list
  rules; already-seen filings are skipped. Empty = no backfill.

  Nightly final pass (23:30–23:45 IST, after the 22:30 recap) —
  retries today's text-posted filings as cards (digest["text_posts"]
  holds key + PDF URL + date) and sweeps digest["today_results"]
  (saved by the morning gate) for missed results. A retry posts a
  NEW card only on a clean parse; anything still failing posts
  nothing. Silent-safe: a gate failure never breaks posting.

Safety rules:
- A filing whose PDF cannot be downloaded is NOT marked seen, so the
  next run retries it (delayed, never lost).
- A filing whose numbers cannot be parsed, or which fails the
  Total Income cross-check, gets a "Results Filed" text post + link
  only, and is then marked seen. Numbers are never invented.
- Filings older than 7 days are skipped (marked seen, not posted).
- The consolidated table in a filing is used when present (user
  decision, 30-Oct-2026); otherwise the standalone table is used as
  a fallback. The card labels which basis it shows. PAT Margin =
  PAT / Revenue, as in filings.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests

from results_parser import parse_results
from card import render_card, card_data_from_parse
from telegram_post import send_photo, send_text

FEED_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
ARCHIVE = "https://nsearchives.nseindia.com/corporate/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MAX_AGE_DAYS = 7

EMOJI = {"Excellent": "🚀", "Good": "🟢", "Positive": "🔵",
         "OK": "🌱", "Weak": "🔴", "Bad": "❌",
         "Results Filed": "📋"}


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"seen": []}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def filing_key(item):
    return item.get("seq_id") or f"{item.get('symbol')}|{item.get('an_dt')}"


def ist_today():
    """Today's date in IST (UTC+5:30), no tzdata dependency."""
    return (datetime.now(timezone.utc)
            + timedelta(hours=5, minutes=30)).date().isoformat()


def ist_now():
    """Current datetime in IST (UTC+5:30), no tzdata dependency."""
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def record_archive(state, symbol, company, rating, basis, quarter_label,
                   sales, pat, eps, filing_url, filing_time=""):
    """Append one posted item to state["archive"] (capped at 300).

    The archive page reads state.json, which the workflow already
    commits, so no workflow change is needed. Entries are channel
    posts — already public — so storing them in the repo is fine.
    "t" is the filing's NSE timestamp (an_dt); the night recap uses
    it for the During/After Market split, and entries without it
    fall back to After Market.
    """
    from telegram_post import tradingview_url
    entries = state.setdefault("archive", [])
    entries.append({
        "d": ist_today(), "s": symbol, "c": company, "r": rating,
        "b": basis or "", "q": quarter_label or "",
        "sales": sales, "pat": pat, "eps": eps,
        "filing": filing_url, "chart": tradingview_url(symbol),
        "t": filing_time or "",
    })
    del entries[:-300]


def fetch_feed():
    r = requests.get(FEED_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("announcements") or []
    return data


SYMBOL_FEED_URL = ("https://www.nseindia.com/api/corporate-announcements"
                   "?index=equities&symbol={symbol}")


def fetch_symbol_feed(symbol, attempts=4):
    """One symbol's full announcement history from NSE.

    NSE 403s intermittently, so retry a few times. A symbol whose
    feed cannot be fetched at all returns [] (skipped, never
    guessed); the caller still runs the normal feed check.
    """
    url = SYMBOL_FEED_URL.format(symbol=symbol)
    last_exc = None
    for _ in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                data = data.get("data") or data.get("announcements") or []
            return data
        except Exception as exc:
            last_exc = exc
            time.sleep(5)
    print(f"[{symbol}] symbol feed unavailable after {attempts} "
          f"attempts ({last_exc})")
    return []


def filed_today(item):
    """True when the filing's an_dt date is today in IST.

    an_dt looks like "31-Oct-2026 13:04:49" (NSE timestamps are
    IST); only the date part is compared, against ist_today().
    """
    raw = (item.get("an_dt") or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat() \
                == ist_today()
        except ValueError:
            continue
    return False


def collect_backfill_items(symbols, state):
    """Today-results filings per symbol, minus already-seen keys.

    Uses the watcher's exact results filter (is_results_filing) and
    exact key construction (filing_key); nothing is broadened.
    """
    seen = set(state.get("seen", []))
    items = []
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        rows = fetch_symbol_feed(symbol)
        found = [it for it in rows
                 if is_results_filing(it) and filed_today(it)]
        new = [it for it in found if filing_key(it) not in seen]
        print(f"[{symbol}] today results filings: {len(found)}; "
              f"new: {len(new)}")
        items.extend(new)
    return items


def is_results_filing(item):
    desc = (item.get("desc") or "")
    text = (item.get("attchmntText") or "")
    return desc.strip().lower() == "outcome of board meeting" and \
        "financial results" in text.lower()


def pdf_url(item):
    f = item.get("attchmntFile") or ""
    if f.startswith("http"):
        return f
    return ARCHIVE + f.lstrip("/")


def extract_text(pdf_bytes):
    """PDF -> text. pypdf on the runner; pdftotext fallback locally."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        pass
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(pdf_bytes)
        path = fh.name
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, timeout=120)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    raise RuntimeError("no PDF text extractor available")


def extract_layout_text(pdf_bytes):
    """PDF -> layout-mode text (pypdf), or None on any failure.

    Layout mode keeps each table row's columns on one line, which
    recovers column-block consolidated tables that the default
    extraction scatters. Only used as a retry when the default parse
    did not yield a Consolidated card, so it never changes the path
    for filings that already parse; a failure here just keeps the
    original result.
    """
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text(extraction_mode="layout") or "")
                         for p in reader.pages)
    except Exception:
        return None


def filing_age_days(item):
    raw = item.get("an_dt") or ""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except ValueError:
            continue
    return 0


def build_caption(symbol, company, parsed, filing_url):
    """One short caption line, SMT-style: all detail lives on the
    image itself (table, reason, disclaimer), so nothing is repeated
    below it. The Result/Chart buttons are attached separately by
    send_photo. No alert-time claim: the 5-minute trigger cannot
    honestly promise seconds."""
    emoji = EMOJI.get(parsed["rating"], "📋")
    return f"#{symbol} — {emoji} {parsed['rating']} Results"


def quarter_month_labels(ql):
    """'Q2 FY27' -> [current, previous, year-ago] short labels.

    Quarter ends: Q1 Jun, Q2 Sep, Q3 Dec, Q4 Mar. The calendar year of
    a Q1/Q2/Q3 end is FY-1; a Q4 end falls in the FY year itself.
    """
    m = re.match(r"Q(\d) FY(\d+)", ql or "")
    if not m:
        return ["Cur", "Prev", "LY"]

    def end_of(q, fy):
        return {1: ("Jun", fy - 1), 2: ("Sep", fy - 1),
                3: ("Dec", fy - 1), 4: ("Mar", fy)}[q]

    q = int(m.group(1))
    fy = 2000 + int(m.group(2))
    cur = end_of(q, fy)
    pq = q - 1 if q > 1 else 4
    prev = end_of(pq, fy if q > 1 else fy - 1)
    ly = end_of(q, fy - 1)
    return [f"{mon}'{yr % 100:02d}" for mon, yr in (cur, prev, ly)]


def process_item(item, token, chat_id, state, text_fallback=True):
    symbol = item.get("symbol") or "?"
    company = item.get("sm_name") or symbol
    url = pdf_url(item)
    key = filing_key(item)
    filing_time = (item.get("an_dt") or "").strip()
    print(f"[{symbol}] {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        pdf = r.content
    except Exception as exc:
        print(f"[{symbol}] download failed ({exc}); will retry next run")
        return False
    try:
        text = extract_text(pdf)
        parsed = parse_results(text)
        if parsed is None or parsed.get("basis") == "Standalone":
            # Consolidated table may still be recoverable from
            # layout-mode text (column-block filings). Accept the
            # retry only when it actually yields Consolidated, so
            # this never downgrades or changes an existing result.
            layout = extract_layout_text(pdf)
            if layout:
                retry = parse_results(text, layout_text=layout)
                if retry is not None and retry.get("basis") == "Consolidated":
                    parsed = retry
        if parsed is None:
            # OCR fallback (OCR batch): fresh OCR bypasses a garbled
            # embedded text layer (FINPIPE, MALUPAPER, BALKRISHNA).
            # Only for refusals — never for filings that already
            # parse — and only a fully guarded parse is accepted.
            try:
                from ocr_fallback import ocr_parse
                ocr = ocr_parse(pdf)
                if ocr is not None:
                    parsed = ocr
                    print(f"[{symbol}] OCR recovered "
                          f"{ocr.get('basis')} card")
                else:
                    print(f"[{symbol}] OCR fallback found nothing usable")
            except Exception as exc:
                print(f"[{symbol}] OCR fallback failed ({exc})")
    except Exception as exc:
        print(f"[{symbol}] extraction failed ({exc})")
        parsed = None
    if parsed is None:
        if not text_fallback:
            # Nightly retry path: a filing that still fails posts
            # nothing — the original text post stands, never
            # duplicated, never a guessed card.
            print(f"[{symbol}] nightly retry still unparsed; "
                  "original text post stands, nothing re-posted")
            return False
        send_text(token, chat_id,
                  f"#{symbol} — 📋 Results Filed\n"
                  f"{company} has filed its quarterly results.\n"
                  f"Filing: {url}\n"
                  "For education only — not investment advice. "
                  "I am not SEBI-registered.")
        import results_parser
        print(f"[{symbol}] posted Results Filed "
              f"(unparsed: {results_parser.LAST_REFUSAL})")
        # Nightly final pass needs the filing key to re-attempt
        # exactly this filing (the archive entry has no key).
        state.setdefault("digest", {}).setdefault("text_posts", []).append({
            "key": key, "seq_id": item.get("seq_id"),
            "symbol": symbol, "company": company, "url": url,
            "an_dt": filing_time, "date": ist_today()})
        if key not in state["seen"]:
            state["seen"].append(key)
        record_archive(state, symbol, company, "Results Filed", "", "",
                       None, None, None, url, filing_time)
        save_state(state)
        return True

    q_labels = quarter_month_labels(parsed["quarter_label"])
    with tempfile.TemporaryDirectory() as td:
        data = card_data_from_parse(company, symbol, parsed, filing_time,
                                    q_labels)
        img = os.path.join(td, f"{symbol}.png")
        render_card(data, img)
        send_photo(token, chat_id, img,
                   build_caption(symbol, company, parsed, url), url, symbol)
    print(f"[{symbol}] posted {parsed['rating']} card")
    if key not in state["seen"]:
        state["seen"].append(key)
    eps = parsed["eps"] or (None, None, None)
    record_archive(state, symbol, company, parsed["rating"],
                   parsed.get("basis", ""), parsed["quarter_label"],
                   parsed["sales"][0], parsed["pat"][0], eps[0], url,
                   filing_time)
    save_state(state)
    return True


def _nightly_retry_entries(token, chat_id, state, entries):
    """Retry text-posted filings through the full pipeline.

    Returns the set of keys that posted as cards. A filing that
    still fails posts nothing (original text post stands).
    Per-item guards: one failure never aborts the rest.
    """
    posted = set()
    for e in entries:
        item = {"symbol": e.get("symbol"),
                "sm_name": e.get("company") or e.get("symbol"),
                "attchmntFile": e.get("url"),
                "an_dt": e.get("an_dt") or "",
                "seq_id": e.get("seq_id")}
        try:
            if filing_age_days(item) > MAX_AGE_DAYS:
                continue
            if process_item(item, token, chat_id, state,
                            text_fallback=False):
                posted.add(e.get("key"))
        except Exception as exc:
            print(f"[{e.get('symbol')}] nightly retry failed ({exc})")
    return posted


def maybe_nightly_pass(token, chat_id, state, save_state):
    """23:30–23:45 IST final pass (after the 22:30 recap, never
    overlapping it): (1) retry today's text-posted filings as
    cards; (2) sweep today's symbols for missed results filings.
    Runs once per day (digest["nightly_pass"] flag, set only on
    completion, so a crashed pass retries at the next run inside
    the window). Silent-safe: per-item guards plus the caller's
    try/except mean a gate failure can never break results
    posting. A retry posts a NEW card message only on a clean
    parse; anything still failing posts nothing.
    """
    dg = state.setdefault("digest", {})
    today = ist_today()
    if dg.get("nightly_pass") == today:
        return
    now = ist_now()
    if not (now.hour == 23 and 30 <= now.minute < 45):
        return
    print(f"[nightly] final pass for {today}")
    # (1) Retry today's text-posted filings.
    entries = [e for e in dg.get("text_posts", [])
               if e.get("date") == today]
    posted = _nightly_retry_entries(token, chat_id, state, entries)
    # (2) Sweep today's symbols for missed results filings.
    saved = dg.get("today_results") or {}
    symbols = saved.get("symbols", []) \
        if saved.get("date") == today else []
    before_keys = {e.get("key") for e in dg.get("text_posts", [])}
    if symbols:
        try:
            items = collect_backfill_items(symbols, state)
            for it in items:
                if filing_key(it) in state["seen"]:
                    continue
                try:
                    process_item(it, token, chat_id, state)
                except Exception as exc:
                    print(f"[{it.get('symbol')}] nightly sweep "
                          f"failed ({exc})")
        except Exception as exc:
            print(f"[nightly] sweep failed ({exc})")
    # Retry text posts created by the sweep itself.
    fresh = [e for e in dg.get("text_posts", [])
             if e.get("key") not in before_keys
             and e.get("date") == today]
    if fresh:
        posted |= _nightly_retry_entries(token, chat_id, state, fresh)
    # Drop retried-as-cards entries and stale entries.
    dg["text_posts"] = [
        e for e in dg.get("text_posts", [])
        if e.get("key") not in posted and e.get("date") == today]
    dg["nightly_pass"] = today
    save_state(state)
    print(f"[nightly] final pass done for {today}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args()

    # Backfill mode: only when BACKFILL_SYMBOLS is non-empty.
    backfill_symbols = [s.strip() for s in
                        os.environ.get("BACKFILL_SYMBOLS", "").split(",")]
    backfill_symbols = [s for s in backfill_symbols if s]

    try:
        feed = fetch_feed()
    except Exception as exc:
        # NSE blocks intermittently (403s come and go). Nothing is
        # written, so the next scheduled run simply retries.
        print(f"NSE feed unavailable ({exc}); will retry next run")
        if not backfill_symbols:
            return 0
        feed = []
    results = [it for it in feed if is_results_filing(it)]
    state = load_state()
    seen = set(state.get("seen", []))
    new = [it for it in results if filing_key(it) not in seen]
    print(f"results filings in feed: {len(results)}; new: {len(new)}")

    backfill = []
    if backfill_symbols:
        print(f"backfill symbols: {', '.join(backfill_symbols)}")
        backfill = collect_backfill_items(backfill_symbols, state)
        print(f"backfill new filings: {len(backfill)}")

    if args.check:
        for it in backfill + new:
            print(f"{it.get('symbol')} | {it.get('sm_name')} | "
                  f"filed {it.get('an_dt')} | {pdf_url(it)}")
        return 0
    if args.seed:
        for it in backfill + new:
            state["seen"].append(filing_key(it))
        save_state(state)
        print(f"seeded {len(backfill) + len(new)} filings; posted nothing")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set",
              file=sys.stderr)
        return 2
    for it in backfill + new:
        # Live re-check: a filing seen via the backfill pass must
        # not post again from the feed pass (or vice versa).
        if filing_key(it) in state["seen"]:
            continue
        if filing_age_days(it) > MAX_AGE_DAYS:
            print(f"[{it.get('symbol')}] older than {MAX_AGE_DAYS}d, skipping")
            state["seen"].append(filing_key(it))
            save_state(state)
            continue
        process_item(it, token, chat_id, state)
    try:
        # Lazy import: a digest bug must never break results posting.
        from digest import maybe_run_digests
        maybe_run_digests(token, chat_id, state, save_state)
    except Exception as exc:
        print(f"[digest] skipped ({exc})")
    try:
        maybe_nightly_pass(token, chat_id, state, save_state)
    except Exception as exc:
        print(f"[nightly] skipped ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
