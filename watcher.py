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
from datetime import datetime, timezone

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


def fetch_feed():
    r = requests.get(FEED_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("announcements") or []
    return data


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


def extract_logo(pdf_bytes):
    """Best logo-like image from the filing's first page, as PNG
    bytes, or None. The logo comes from the company's own filing
    (letterhead), so no external logo library or symbol->domain
    mapping is needed. Strict shape rules keep the wrong images out:
    full-page scans (tall images) are skipped, as are signatures
    and address-text blocks; only wide letterhead banners or
    near-square logo marks pass. When nothing passes, the caller
    falls back to the initials badge, so a missing logo never
    breaks a card. A seal or mark that passes the shape test is
    still the company's own artwork, not another company's logo.
    """
    try:
        from pypdf import PdfReader
        from PIL import Image as _Im
        import io as _io
        reader = PdfReader(_io.BytesIO(pdf_bytes))
        if not reader.pages:
            return None
        best, best_area = None, 0
        for im in reader.pages[0].images:
            try:
                img = _Im.open(_io.BytesIO(im.data))
                img.load()
            except Exception:
                continue
            w, h = img.size
            if h > 1000:
                continue
            aspect = w / h if h else 0
            banner = (w >= 400 and 60 <= h <= 500
                      and 2.0 <= aspect <= 9.0)
            mark = (120 <= w <= 450 and 120 <= h <= 450
                    and 0.75 <= aspect <= 1.33)
            if not (banner or mark):
                continue
            if w * h > best_area:
                best, best_area = img, w * h
        if best is None:
            return None
        buf = _io.BytesIO()
        best.convert("RGBA").save(buf, format="PNG")
        return buf.getvalue()
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


def process_item(item, token, chat_id, state):
    symbol = item.get("symbol") or "?"
    company = item.get("sm_name") or symbol
    url = pdf_url(item)
    key = filing_key(item)
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
    except Exception as exc:
        print(f"[{symbol}] extraction failed ({exc})")
        parsed = None
    if parsed is None:
        send_text(token, chat_id,
                  f"#{symbol} — 📋 Results Filed\n"
                  f"{company} has filed its quarterly results.\n"
                  f"Filing: {url}\n"
                  "For education only — not investment advice. "
                  "I am not SEBI-registered.")
        print(f"[{symbol}] posted Results Filed (unparsed)")
        state["seen"].append(key)
        save_state(state)
        return True

    filing_time = (item.get("an_dt") or "").strip()
    q_labels = quarter_month_labels(parsed["quarter_label"])
    with tempfile.TemporaryDirectory() as td:
        logo_path = None
        logo_bytes = extract_logo(pdf)
        if logo_bytes:
            logo_path = os.path.join(td, "logo.png")
            with open(logo_path, "wb") as fh:
                fh.write(logo_bytes)
        data = card_data_from_parse(company, symbol, parsed, filing_time,
                                    q_labels, logo_path=logo_path)
        img = os.path.join(td, f"{symbol}.png")
        render_card(data, img)
        send_photo(token, chat_id, img,
                   build_caption(symbol, company, parsed, url), url, symbol)
    print(f"[{symbol}] posted {parsed['rating']} card")
    state["seen"].append(key)
    save_state(state)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args()

    try:
        feed = fetch_feed()
    except Exception as exc:
        # NSE blocks intermittently (403s come and go). Nothing is
        # written, so the next scheduled run simply retries.
        print(f"NSE feed unavailable ({exc}); will retry next run")
        return 0
    results = [it for it in feed if is_results_filing(it)]
    state = load_state()
    seen = set(state.get("seen", []))
    new = [it for it in results if filing_key(it) not in seen]
    print(f"results filings in feed: {len(results)}; new: {len(new)}")

    if args.check:
        for it in new:
            print(f"{it.get('symbol')} | {it.get('sm_name')} | "
                  f"filed {it.get('an_dt')} | {pdf_url(it)}")
        return 0
    if args.seed:
        for it in new:
            state["seen"].append(filing_key(it))
        save_state(state)
        print(f"seeded {len(new)} filings; posted nothing")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set",
              file=sys.stderr)
        return 2
    for it in new:
        if filing_age_days(it) > MAX_AGE_DAYS:
            print(f"[{it.get('symbol')}] older than {MAX_AGE_DAYS}d, skipping")
            state["seen"].append(filing_key(it))
            save_state(state)
            continue
        process_item(it, token, chat_id, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
