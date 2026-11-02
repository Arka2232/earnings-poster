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

  XBRL Phase 1 recovery — when a filing posts as text/screenshot
  (the PDF chain failed), the filing is recorded for XBRL retry
  (text_posts entries carry "qe", the inferred quarter-end, and
  "basis", the basis attempted — "" when the PDF never yielded
  one). Later runs (throttled to one attempt per hour per entry)
  and the nightly pass (forced) query NSE's integrated-filing
  results API, windowed around the filing date, and build the card
  from the filing's own XBRL document under ALL the XBRL guards.
  A recovered card posts as a NEW message below the original and
  the text_posts entry is removed, so nightly never double-posts;
  the archive entry keeps filing = the PDF URL (the "Result"
  button still points at the PDF announcement) plus an "xbrl"
  field with the XBRL URL. Entries expire after 7 days, the same
  age rule as PDF retries. XBRL is recovery, NOT a replacement
  for PDF-first posting (it lags the PDF by 0.1-6.2h). The iron
  disagreement rule in attempt_xbrl_recovery governs every
  XBRL-vs-PDF conflict; banking and non-Ind-AS taxonomies are
  refused, never mapped by guesswork.

Safety rules:
- A filing whose PDF cannot be downloaded — or whose download is
  truncated/corrupt (no final %%EOF marker) — is NOT marked seen,
  so the next run retries it (delayed, never lost). It is never
  text-posted as a substitute for a download that failed.
- A filing whose numbers cannot be parsed, or which fails the
  Total Income cross-check, first tries a tight screenshot of the
  filing's own results-table page (table only, unedited pixels —
  no retyped numbers, no rating); if no confident tight crop is
  found, it gets a clean "Results Filed" text post + link only,
  never a messy full-page photo, and is then marked seen.
  Numbers are never invented.
- No duplicates, at any dispatch cadence: postings are deduped by
  filing key AND by filing URL (same filing under a different
  seq_id), each run resyncs state from origin before posting,
  each post is pre-checked against the latest pushed state, and
  the workflow's Save-state step semantically merges state on a
  push conflict instead of losing the run's postings.
- Filings older than 7 days are skipped (marked seen, not posted).
- The consolidated table in a filing is used when present (user
  decision, 30-Oct-2026); otherwise the standalone table is used as
  a fallback. The card labels which basis it shows. PAT Margin =
  PAT / Revenue, as in filings.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests

from results_parser import parse_results, verdict
from card import render_card, card_data_from_parse
from telegram_post import send_photo, send_text
from xbrl_parser import (build_card as xbrl_build_card, select_xbrl_row,
                         fetch_integrated_rows, download_xbrl,
                         detect_taxonomy)
import xbrl_parser

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


def filing_url_key(url):
    """Stable identity of a filing by its PDF file, independent of
    seq_id. The same NSE filing can surface under a different
    seq_id (re-listed, or feed vs per-symbol history disagreeing),
    and a seq_id-only seen-list then posts it twice. The PDF
    filename embeds symbol + filing timestamp, so its basename is
    a content-stable key; lowercased and query-stripped so URL
    form differences (full URL vs archive prefix) cannot split it.
    """
    if not url:
        return ""
    base = (url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return base.lower()


def archive_url_keys(state):
    """urlkeys of every filing already recorded in the archive,
    split by whether it was posted as a real card or only as a
    'Results Filed' placeholder (text/screenshot). A card is the
    final form: nothing further may post for that filing. A
    placeholder does NOT block the nightly card retry — that
    upgrade is the intended path, not a duplicate."""
    cards, placeholders = set(), set()
    for e in state.get("archive", []):
        k = filing_url_key(e.get("filing") or "")
        if not k:
            continue
        if (e.get("r") or "") == "Results Filed":
            placeholders.add(k)
        else:
            cards.add(k)
    for k in state.get("seen_urls", []) or []:
        # seen_urls only records postings; a key there with no
        # archive entry is treated as a placeholder, never a card,
        # so a card retry is never blocked by it.
        if k not in cards:
            placeholders.add(k)
    return cards, placeholders


def resync_state_from_git(state):
    """Best-effort: pull the latest pushed state before posting.

    At 1-minute dispatch cadence runs queue and overlap; a run
    that checked out state before a previous run pushed would
    otherwise repost that run's filings. A `git pull --rebase`
    at run start (the runner checkout has push credentials and
    an unmodified state.json at this point, so the pull is
    clean) plus reloading state.json closes that window for any
    run that starts after the previous run pushed. Silent no-op
    outside a git checkout (local runs, tests) or on any failure
    — a failed resync must never block posting, it only means
    the other guards (url dedup, pre-send remote check, push
    retry in the workflow) carry the run.
    """
    try:
        repo = os.path.dirname(os.path.abspath(STATE_FILE))
        if not os.path.isdir(os.path.join(repo, ".git")):
            return
        out = subprocess.run(["git", "-C", repo, "pull", "--rebase"],
                             capture_output=True, timeout=60)
        if out.returncode != 0:
            return
        fresh = load_state()
        state.clear()
        state.update(fresh)
        print("[dedup] state resynced from origin before posting")
    except Exception as exc:
        print(f"[dedup] git resync skipped ({exc})")


REMOTE_STATE_URL = ("https://raw.githubusercontent.com/Arka2232/"
                    "earnings-poster/main/state.json")


def remote_card_url_keys():
    """urlkeys of filings already posted as cards according to the
    latest PUSHED state.json (raw.githubusercontent, no auth).
    Best-effort pre-send check inside process_item: if an
    overlapping run already pushed a card for this filing, this
    run skips it instead of double-posting. Returns None when the
    fetch/parsing fails — the caller then proceeds (a failed
    check must never block a legitimate post).
    """
    try:
        r = requests.get(REMOTE_STATE_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        cards, _ = archive_url_keys(data if isinstance(data, dict)
                                    else {})
        return cards
    except Exception:
        return None


def ist_today():
    """Today's date in IST (UTC+5:30), no tzdata dependency."""
    return (datetime.now(timezone.utc)
            + timedelta(hours=5, minutes=30)).date().isoformat()


def ist_now():
    """Current datetime in IST (UTC+5:30), no tzdata dependency."""
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def record_archive(state, symbol, company, rating, basis, quarter_label,
                   sales, pat, eps, filing_url, filing_time="",
                   xbrl=""):
    """Append one posted item to state["archive"] (capped at 300).

    The night recap reads state["archive"], which the workflow already
    commits, so no workflow change is needed. Entries are channel
    posts — already public — so storing them in the repo is fine.
    "t" is the filing's NSE timestamp (an_dt); the night recap uses
    it for the During/After Market split, and entries without it
    fall back to After Market. "xbrl" is set only for XBRL-recovered
    cards (the filing's own XBRL URL); plain PDF cards carry no
    "xbrl" key, so existing archive entries are byte-identical.
    """
    from telegram_post import tradingview_url
    entries = state.setdefault("archive", [])
    entry = {
        "d": ist_today(), "s": symbol, "c": company, "r": rating,
        "b": basis or "", "q": quarter_label or "",
        "sales": sales, "pat": pat, "eps": eps,
        "filing": filing_url, "chart": tradingview_url(symbol),
        "t": filing_time or "",
    }
    if xbrl:
        entry["xbrl"] = xbrl
    entries.append(entry)
    del entries[:-300]


def record_text_post(state, key, seq_id, symbol, company, url,
                     filing_time):
    """Append one text_posts entry (text OR screenshot fallback).

    The nightly pass and the XBRL retry both read this shape.
    "qe" is the quarter-end in NSE API format ("30-SEP-2026"),
    inferred from the filing date as the last quarter-end
    strictly before it (results are never filed the same day
    the quarter ends, so a filing dated exactly on a quarter-end
    belongs to the previous quarter); None when the filing date
    does not parse, in which case the XBRL retry falls back to
    broadcast-proximity selection. "basis" is the basis the PDF
    path attempted — "" when the PDF never yielded one (the
    normal fallback case); the XBRL retry applies its own
    Consolidated-first policy regardless of this field.
    """
    state.setdefault("digest", {}).setdefault("text_posts", []).append({
        "key": key, "seq_id": seq_id, "symbol": symbol,
        "company": company, "url": url, "an_dt": filing_time,
        "date": ist_today(), "qe": infer_qe_date(filing_time),
        "basis": ""})


def fetch_feed():
    """Latest-20 feed, with retries: NSE 403s intermittently, and
    a single-attempt failure used to lose the whole run (and, at
    1-minute cadence, waste a dispatch). Retries are bounded
    (3 attempts, short backoff) so a genuinely dead feed still
    fails fast instead of hanging the run."""
    last_exc = None
    for attempt in range(3):
        try:
            r = requests.get(FEED_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                data = data.get("data") or data.get("announcements") or []
            return data
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2)
    raise last_exc


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
    cards, placeholders = archive_url_keys(state)
    already = cards | placeholders
    items = []
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        rows = fetch_symbol_feed(symbol)
        found = [it for it in rows
                 if is_results_filing(it) and filed_today(it)]
        new = [it for it in found
               if filing_key(it) not in seen
               and filing_url_key(pdf_url(it)) not in already]
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


def _heading_match(line, consolidated, strict):
    """A results-statement heading line, or False.

    Requires 'financial results' (+ 'consolidated' when asked) on one
    line. Strict mode additionally requires the line to start with
    'statement' (the parser's own guard against covering letters).
    Lines mentioning a review ('We have reviewed the accompanying
    Statement ...') are auditor prose, never a table heading, so
    they are excluded at every strictness — otherwise the loose
    pass would pick an auditor-report page (NILAINFRA p11) over the
    real table.
    """
    low = line.strip().lower()
    if "review" in low or "accompanying" in low:
        return False
    if "financial results" not in low:
        return False
    if consolidated and "consolidated" not in low:
        return False
    if strict and not low.startswith("statement"):
        return False
    if not strict and "statement" not in low:
        return False
    return True


def find_results_page(pdf_bytes):
    """0-based index of the page holding the results table, or None.

    Preference: consolidated statement heading page, then any
    statement heading page, then the first page with a revenue row.
    Strictness degrades (heading must start the line, then the
    words may sit anywhere on the line) to survive extraction
    quirks. None means "not confidently identified" and the caller
    falls back to the text post.
    """
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception:
        return None
    for strict in (True, False):
        for consolidated in (True, False):
            for i, text in enumerate(pages):
                if any(_heading_match(ln, consolidated, strict)
                       for ln in text.splitlines()):
                    return i
    rev_rx = re.compile(
        r"revenue\s+from\s+operations|income\s+from\s+operations",
        re.IGNORECASE)
    for i, text in enumerate(pages):
        if any(rev_rx.search(ln) for ln in text.splitlines()):
            return i
    return None


def render_page_png(pdf_bytes, page_index, out_dir):
    """Render one page (0-based) to PNG with pdftoppm at 200 DPI.

    Returns the PNG path, or None when pdftoppm is missing, the
    render fails, or no output appears. Never raises.
    """
    pdf_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf",
                                         delete=False) as fh:
            fh.write(pdf_bytes)
            pdf_path = fh.name
        prefix = os.path.join(out_dir, "page")
        out = subprocess.run(
            ["pdftoppm", "-png", "-r", "200",
             "-f", str(page_index + 1), "-l", str(page_index + 1),
             pdf_path, prefix],
            capture_output=True, timeout=180)
        if out.returncode != 0:
            return None
        cands = sorted(glob.glob(prefix + "*.png"))
        if not cands:
            return None
        path = cands[0]
        if os.path.getsize(path) == 0:
            return None
        return path
    except Exception:
        return None
    finally:
        if pdf_path:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass


def _tsv_lines(png_path):
    """OCR line boxes via tesseract TSV: [(text, top, bottom)].

    Words (level 5) are grouped by (block, par, line); a line's box
    spans its words. Returns [] when tesseract is missing/fails.
    """
    try:
        out = subprocess.run(["tesseract", png_path, "stdout", "tsv"],
                             capture_output=True, timeout=180)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    import csv
    import io as _io
    lines = {}
    order = []
    reader = csv.DictReader(
        _io.StringIO(out.stdout.decode("utf-8", "replace")),
        delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        try:
            if int(row.get("level", 0)) != 5:
                continue
            key = (row.get("block_num"), row.get("par_num"),
                   row.get("line_num"))
            left = int(row["left"])
            top = int(row["top"])
            right = left + int(row["width"])
            bottom = top + int(row["height"])
            word = (row.get("text") or "").strip()
        except (ValueError, TypeError, KeyError):
            continue
        if key not in lines:
            lines[key] = {"words": [], "top": top, "bottom": bottom}
            order.append(key)
        if word:
            lines[key]["words"].append(word)
        lines[key]["top"] = min(lines[key]["top"], top)
        lines[key]["bottom"] = max(lines[key]["bottom"], bottom)
    result = []
    for key in order:
        entry = lines[key]
        if entry["words"]:
            result.append((" ".join(entry["words"]),
                           entry["top"], entry["bottom"]))
    return result


def _line_words(text):
    words = set()
    for w in text.lower().split():
        words.add(w.strip(".,;:()[]\"'"))
    return words


def _first_token(text):
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.lower().split(None, 1)[0].strip(".,;:()[]\"'")


def crop_table_bounds(png_path):
    """Vertical crop (top, bottom) for the results table, or None.

    Top = the 'Statement of ... Financial Results' heading line;
    bottom = the last Basic/Diluted row of the EPS block (or the EPS
    header line itself), else the last table row before the Notes
    section or the signature block. None means "not confidently
    located" — the caller posts clean text instead (never a full
    page). The bounds always include the whole table
    (over-inclusion beats a cut table); a wrong crop is never
    returned. Never raises.
    """
    try:
        lines = _tsv_lines(png_path)
        if not lines:
            return None
        head_idx = None
        for i, (text, _top, _bottom) in enumerate(lines):
            if _first_token(text) == "statement":
                words = _line_words(text)
                if "financial" in words and "results" in words:
                    head_idx = i
                    break
        if head_idx is None:
            for i, (text, _top, _bottom) in enumerate(lines):
                words = _line_words(text)
                if ("statement" in words and "financial" in words
                        and "results" in words):
                    head_idx = i
                    break
        if head_idx is None:
            return None
        eps_idx = None
        for i in range(head_idx + 1, len(lines)):
            words = _line_words(lines[i][0])
            if (any(w.startswith("earning") for w in words)
                    and "share" in words):
                eps_idx = i
                break
        if eps_idx is not None:
            # EPS can span several blocks (EXXARO: continuing,
            # discontinued, then combined) — take the LAST
            # Basic/Diluted row, stopping at Notes/signature
            # markers so notes prose never extends the crop.
            bottom_idx = eps_idx
            for i in range(eps_idx + 1, min(len(lines), eps_idx + 41)):
                text = lines[i][0]
                words = _line_words(text)
                first = _first_token(text)
                if (first in ("notes", "note", "place", "date")
                        or "on behalf of" in text.lower()
                        or "din" in words):
                    break
                if (any(w.startswith("basic") for w in words)
                        or any(w.startswith("diluted") for w in words)):
                    bottom_idx = i
        else:
            bottom_idx = None
            for i in range(head_idx + 1, len(lines)):
                text = lines[i][0]
                words = _line_words(text)
                first = _first_token(text)
                if (first in ("notes", "note", "place", "date")
                        or "on behalf of" in text.lower()
                        or "din" in words):
                    break
                nums = [tok for tok in text.split()
                        if any(ch.isdigit() for ch in tok)]
                if len(nums) >= 2:
                    bottom_idx = i
            if bottom_idx is None or bottom_idx <= head_idx:
                return None
        top = lines[head_idx][1]
        bottom = lines[bottom_idx][2]
        if bottom <= top:
            return None
        return (top, bottom)
    except Exception:
        return None


def crop_table_image(png_path):
    """Crop the rendered page to the table (heading..EPS/last row).

    Returns True when the image was cropped, False when it must be
    left alone (boundaries unconfident, crop implausibly tall, or
    any step failed) — the caller then drops the photo entirely
    and posts text instead of a messy full page.
    The original file is never clobbered: the crop is written to a
    side file and moved over only on success. Never raises.
    """
    try:
        bounds = crop_table_bounds(png_path)
        if bounds is None:
            return False
        from PIL import Image
        im = Image.open(png_path)
        im.load()
        w, h = im.size
        top = max(0, bounds[0] - 12)
        bottom = min(h, bounds[1] + 12)
        if bottom - top < 0.08 * h or top >= bottom:
            return False
        if bottom - top > 0.92 * h:
            # A crop spanning nearly the whole page is not a table
            # crop at all — the bounds almost certainly swallowed
            # the header band, notes, or signature/stamp blocks.
            # Treat as unconfident: caller falls back to text.
            return False
        tmp_path = png_path + ".crop.png"
        im.crop((0, top, w, bottom)).save(tmp_path)
        if os.path.getsize(tmp_path) == 0:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
        os.replace(tmp_path, png_path)
        return True
    except Exception:
        return False


def results_screenshot_path(pdf_bytes, out_dir):
    """Path to a tight results-table screenshot PNG, or None.

    Finds the table page, renders it at 200 DPI, and crops it
    vertically to the table (heading -> EPS row). PROFESSIONAL
    RULE: only a tight, clean table crop is ever posted. A crop
    failure returns None (caller posts clean text instead) —
    NEVER a full page carrying the company header/logo band,
    notes, or signature/stamp blocks; a messy full-page photo
    looks like a failure and is worse than no photo. A
    page-find or render failure likewise returns None.
    Never raises.
    """
    try:
        page = find_results_page(pdf_bytes)
        if page is None:
            return None
        path = render_page_png(pdf_bytes, page, out_dir)
        if path is None:
            return None
        if not crop_table_image(path):
            # Fail LOUD in logs (not in the post): a missing
            # tesseract binary makes every crop fail, which
            # silently turns every screenshot fallback into text.
            # That environment failure must be visible, since the
            # crop-only rule means no photo is posted at all.
            import shutil
            if shutil.which("tesseract") is None:
                print("[screenshot] tesseract missing: crop impossible, "
                      "falling back to text post")
            return None
        return path
    except Exception:
        return None


def pdf_looks_valid(pdf_bytes):
    """True when downloaded bytes look like a complete PDF.

    CINELINE (run #348): NSE served a TRUNCATED PDF (missing
    %%EOF); pypdf raised "EOF marker not found", extract_text
    raised, and the filing fell through to a text post marked
    seen — a transient download fault became a permanent
    text-only post. A truncated download must instead be treated
    as a download failure (no seen-mark, retried next run).
    Checks: non-trivial size, %PDF header, and a %%EOF marker
    as the file's final marker (trailing whitespace allowed).
    The EOF marker is REQUIRED and ANCHORED at the end, not
    merely present somewhere: pypdf tolerates a missing final
    EOF (it still lists pages from a truncated file), and the
    %%EOF byte string can also occur mid-file in an
    incrementally updated PDF — both loopholes let the
    truncated CINELINE file through in earlier drafts of this
    check. A file whose last marker is not %%EOF is corrupt
    even if some tool could squeeze partial text from it;
    partial text is exactly how wrong cards happen, so it is
    never parsed.
    """
    if not pdf_bytes or len(pdf_bytes) < 1024:
        return False
    if not pdf_bytes.lstrip()[:4] == b"%PDF":
        return False
    return pdf_bytes.rstrip()[-5:] == b"%%EOF"


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
    ukey = filing_url_key(url)
    filing_time = (item.get("an_dt") or "").strip()
    print(f"[{symbol}] {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        pdf = r.content
    except Exception as exc:
        print(f"[{symbol}] download failed ({exc}); will retry next run")
        return False
    if not pdf_looks_valid(pdf):
        # Truncated/corrupt download (CINELINE): NOT marked seen,
        # NOT text-posted — the next run re-downloads. A text post
        # here would bury a filing whose real numbers never got a
        # chance to parse.
        print(f"[{symbol}] PDF invalid/truncated; will retry next run")
        return False
    # Extraction chain, each stage in its OWN try block (CINELINE
    # lesson): the old single try block let an extract_text
    # exception skip the layout retry AND the OCR fallback. Now
    # an extraction failure still attempts OCR (which renders the
    # PDF directly and does not depend on the text layer), and
    # only when every stage fails does the filing fall through
    # to screenshot/text below.
    parsed = None
    text = None
    try:
        text = extract_text(pdf)
    except Exception as exc:
        print(f"[{symbol}] extraction failed ({exc}); trying OCR")
    if text is not None:
        try:
            parsed = parse_results(text)
        except Exception as exc:
            print(f"[{symbol}] parse failed ({exc})")
            parsed = None
        if parsed is None or parsed.get("basis") == "Standalone":
            # Consolidated table may still be recoverable from
            # layout-mode text (column-block filings). Accept the
            # retry only when it actually yields Consolidated, so
            # this never downgrades or changes an existing result.
            try:
                layout = extract_layout_text(pdf)
            except Exception:
                layout = None
            if layout:
                try:
                    retry = parse_results(text, layout_text=layout)
                except Exception:
                    retry = None
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
    if parsed is None:
        if not text_fallback:
            # Nightly retry path: a filing that still fails posts
            # nothing — the original text post stands, never
            # duplicated, never a guessed card.
            print(f"[{symbol}] nightly retry still unparsed; "
                  "original text post stands, nothing re-posted")
            return False
        # Duplicate guard (placeholder path): a filing that already
        # has ANY archive entry (card or placeholder) never gets a
        # second placeholder post — this is the same-filing-under-
        # a-different-key case. Record the key locally and stop.
        cards2, placeholders2 = archive_url_keys(state)
        remote_cards2 = remote_card_url_keys()
        if ukey and (ukey in cards2 or ukey in placeholders2
                     or (remote_cards2 is not None
                         and ukey in remote_cards2)):
            print(f"[{symbol}] already posted for this filing; skipping")
            if key not in state["seen"]:
                state["seen"].append(key)
            if ukey not in state.setdefault("seen_urls", []):
                state["seen_urls"].append(ukey)
            save_state(state)
            return False
        # Screenshot fallback: the filing's own results-table page,
        # cropped tightly to the table only (heading -> EPS row).
        # Unedited source pixels — no retyped numbers, no rating,
        # no branding added. A crop failure posts the clean text
        # post below instead of a messy full page; a screenshot
        # failure never breaks posting. The filing is recorded in
        # text_posts exactly like a text post, so the nightly
        # retry still tries it as a card.
        with tempfile.TemporaryDirectory() as td:
            shot = results_screenshot_path(pdf, td)
            if shot is not None:
                send_photo(token, chat_id, shot,
                           f"#{symbol} — 📋 Results Filed\n"
                           f"{company} has filed its quarterly results. "
                           "The structured card follows once this filing "
                           "can be read.\n"
                           "For education only — not investment advice. "
                           "I am not SEBI-registered.",
                           url, symbol)
                import results_parser
                print(f"[{symbol}] posted Results Filed screenshot "
                      f"(unparsed: {results_parser.LAST_REFUSAL})")
                state.setdefault("digest", {}).setdefault(
                    "text_posts", [])
                record_text_post(state, key, item.get("seq_id"), symbol,
                                 company, url, filing_time)
                if key not in state["seen"]:
                    state["seen"].append(key)
                if ukey and ukey not in state.setdefault("seen_urls", []):
                    state["seen_urls"].append(ukey)
                record_archive(state, symbol, company, "Results Filed",
                               "", "", None, None, None, url, filing_time)
                save_state(state)
                return True
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
        record_text_post(state, key, item.get("seq_id"), symbol, company,
                         url, filing_time)
        if key not in state["seen"]:
            state["seen"].append(key)
        if ukey and ukey not in state.setdefault("seen_urls", []):
            state["seen_urls"].append(ukey)
        record_archive(state, symbol, company, "Results Filed", "", "",
                       None, None, None, url, filing_time)
        save_state(state)
        return True

    # Duplicate guard (card path): never post a second card for a
    # filing that already has one — locally, or in the latest
    # pushed state (an overlapping run may have posted + pushed
    # after this run loaded state). A "Results Filed" placeholder
    # does NOT block: the nightly card retry upgrades it. When
    # blocked, record the key locally (no post, no archive entry)
    # so this run's own state push converges instead of retrying
    # the same filing forever.
    cards, _ = archive_url_keys(state)
    remote_cards = remote_card_url_keys()
    if ukey and (ukey in cards
                 or (remote_cards is not None and ukey in remote_cards)):
        print(f"[{symbol}] card already posted for this filing; skipping")
        if key not in state["seen"]:
            state["seen"].append(key)
        if ukey and ukey not in state.setdefault("seen_urls", []):
            state["seen_urls"].append(ukey)
        save_state(state)
        return False
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
    if ukey and ukey not in state.setdefault("seen_urls", []):
        state["seen_urls"].append(ukey)
    eps = parsed["eps"] or (None, None, None)
    record_archive(state, symbol, company, parsed["rating"],
                   parsed.get("basis", ""), parsed["quarter_label"],
                   parsed["sales"][0], parsed["pat"][0], eps[0], url,
                   filing_time)
    save_state(state)
    return True


# ================= XBRL Phase 1 recovery =================
# A filing that posts as text/screenshot (PDF chain failed) is
# retried from its own XBRL document on later runs (throttled)
# and in the nightly pass (forced). See the module docstring.
# All network entry points are watcher-namespace names so tests
# can mock them; no function here ever raises out of
# maybe_xbrl_retry — a failed recovery is a pending entry, never
# a broken run.

_MONTH_ABBR = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_NUM = {m: i + 1 for i, m in enumerate(_MONTH_ABBR)}
XBRL_RETRY_MINUTES = 60
XBRL_MONEY_TOL = 0.05  # Rs Cr; XBRL facts are exact, PDF is rounded
XBRL_EPS_TOL = 0.01


def parse_an_dt_date(raw):
    """NSE an_dt -> date, or None (tries both known formats)."""
    raw = (raw or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def infer_qe_date(an_dt_raw):
    """Filing date -> quarter-end in NSE API format ("30-SEP-2026").

    The last quarter-end STRICTLY before the filing date: results
    are never filed the same day the quarter ends, so a filing
    dated exactly on a quarter-end belongs to the previous
    quarter. None when the filing date does not parse.
    """
    d = parse_an_dt_date(an_dt_raw)
    if d is None:
        return None
    cands = []
    for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
        for year in (d.year, d.year - 1):
            try:
                q = datetime(year, month, day).date()
            except ValueError:
                continue
            if q < d:
                cands.append(q)
    if not cands:
        return None
    q = max(cands)
    return f"{q.day:02d}-{_MONTH_ABBR[q.month - 1]}-{q.year:04d}"


def qe_api_to_iso(qe_api):
    """'30-SEP-2026' -> '2026-09-30', or None on a bad value."""
    try:
        day, mon, year = (qe_api or "").strip().split("-")
        return f"{int(year):04d}-{_MONTH_NUM[mon.upper()]:02d}" \
               f"-{int(day):02d}"
    except (ValueError, KeyError):
        return None


def iso_to_qe_api(iso):
    """'2026-09-30' -> '30-SEP-2026', or None on a bad value."""
    try:
        year, month, day = (int(x) for x in (iso or "").split("-"))
        return f"{day:02d}-{_MONTH_ABBR[month - 1]}-{year:04d}"
    except (ValueError, IndexError):
        return None


def quarter_label_from_iso(iso):
    """'2026-09-30' -> 'Q2 FY27' (same mapping as results_parser)."""
    try:
        year, month, _day = (int(x) for x in (iso or "").split("-"))
    except ValueError:
        return None
    if 4 <= month <= 6:
        q, fy = "Q1", year + 1
    elif 7 <= month <= 9:
        q, fy = "Q2", year + 1
    elif 10 <= month <= 12:
        q, fy = "Q3", year + 1
    else:
        q, fy = "Q4", year
    return f"{q} FY{fy % 100:02d}"


def prior_year_iso(iso):
    """Same quarter-end one year earlier ('2026-09-30'->'2025-09-30')."""
    try:
        year, month, day = (int(x) for x in (iso or "").split("-"))
        return f"{year - 1:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def prev_quarter_end_iso(iso):
    """Previous quarter-end ('2026-09-30'->'2026-06-30')."""
    try:
        year, month, _day = (int(x) for x in (iso or "").split("-"))
    except ValueError:
        return None
    prev = {3: (year - 1, 12, 31), 6: (year, 3, 31),
            9: (year, 6, 30), 12: (year, 9, 30)}.get(month)
    if prev is None:
        return None
    py, pm, pd = prev
    return f"{py:04d}-{pm:02d}-{pd:02d}"


def xbrl_entry_age_days(entry):
    """Entry age in days (an_dt first, posting date as fallback).

    999 when neither date parses: an entry with no usable date is
    corrupt and must expire rather than retry forever.
    """
    today = datetime.strptime(ist_today(), "%Y-%m-%d").date()
    d = parse_an_dt_date(entry.get("an_dt") or "")
    if d is not None:
        return (today - d).days
    try:
        d = datetime.strptime(entry.get("date") or "", "%Y-%m-%d").date()
        return (today - d).days
    except ValueError:
        return 999


def xbrl_entry_date_stale(entry, today_str):
    """True when the posting date is over 7 days old.

    Checked BEFORE any network call, so stale entries (e.g. the
    nightly prune's leftovers) never cost an API request. A
    missing/unparseable posting date is not stale by itself —
    the an_dt age check decides those.
    """
    try:
        d = datetime.strptime(entry.get("date") or "",
                              "%Y-%m-%d").date()
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
        return (today - d).days > MAX_AGE_DAYS
    except ValueError:
        return False


def xbrl_entry_due(entry, now):
    """True when the entry may be attempted now (throttle).

    One attempt per hour per entry on normal runs; the nightly
    pass bypasses this with force=True. An unparseable stamp is
    treated as due (a corrupt stamp must not block recovery).
    """
    raw = entry.get("xbrl_try")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    # ist_now() is tz-aware while a stored stamp may be naive (or
    # vice versa after a code change): compare naive-vs-naive so
    # a tz mismatch can never crash the gate (a crash here would
    # silently skip every retry, as the gate swallows errors).
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - last).total_seconds() >= XBRL_RETRY_MINUTES * 60


def _stamp_xbrl_try(entry):
    entry["xbrl_try"] = ist_now().isoformat()


def _integrated_rows(symbol, from_d, to_d):
    """Windowed integrated-filing rows (equities, then SME fallback).

    An empty equities window is retried once under index=sme:
    SME filers use the same endpoint shape, and a genuinely
    empty window costs one extra call, throttled per entry.
    Raises on persistent failure (caller treats as pending).
    """
    rows = fetch_integrated_rows(symbol, from_date=from_d, to_date=to_d)
    if not rows:
        rows = fetch_integrated_rows(symbol, from_date=from_d,
                                     to_date=to_d, index="sme")
    return rows


def pick_xbrl_row_for_entry(rows, entry):
    """(row, basis) for one pending entry, or (None, None).

    Basis policy, same as the PDF path: Consolidated first,
    Standalone fallback. Selection is by the entry's recorded
    quarter-end; when that yields nothing (e.g. the inferred
    quarter-end was wrong for a delayed filing), the fallback
    picks the Financials row whose broadcast is closest to the
    filing's an_dt within 7 days and re-applies the basis
    policy inside that row's quarter. Governance rows are never
    selected (select_xbrl_row filters the type), and rows
    without an XBRL URL are never selected either.
    """
    qe = entry.get("qe") or infer_qe_date(entry.get("an_dt") or "")
    if qe:
        for basis in ("Consolidated", "Standalone"):
            row = select_xbrl_row(rows, qe, basis)
            if row is not None:
                return row, basis
    an_dt = parse_an_dt_date(entry.get("an_dt") or "")
    if an_dt is not None:
        an_dt = datetime(an_dt.year, an_dt.month, an_dt.day)
        best = []
        for r in rows:
            if r.get("type") != "Integrated Filing- Financials":
                continue
            if not r.get("xbrl"):
                continue
            try:
                bc = datetime.strptime(r.get("broadcast_Date") or "",
                                       "%d-%b-%Y %H:%M:%S")
            except ValueError:
                continue
            gap = abs((bc - an_dt).total_seconds())
            if gap <= 7 * 86400:
                best.append((gap, r))
        if best:
            best.sort(key=lambda t: t[0])
            qe2 = best[0][1].get("qe_Date")
            for basis in ("Consolidated", "Standalone"):
                row = select_xbrl_row(rows, qe2, basis)
                if row is not None:
                    return row, basis
    return None, None


def resolve_xbrl_row(rows, row, basis):
    """(row, xml_bytes, refusal) for the selected row.

    When both an Original and a Revision exist for the same
    quarter+basis, BOTH downloads are compared: identical bytes
    -> the Original is used; differing bytes -> refusal
    ("revision-differs"), never a blind pick. A lone Revision
    (no Original) is used as-is — there is nothing to compare
    against. Download failures raise (caller treats as pending).
    """
    qe_api = row.get("qe_Date")
    if row.get("type_Sub") == "Original":
        revs = [r for r in rows
                if r.get("type") == "Integrated Filing- Financials"
                and r.get("qe_Date") == qe_api
                and r.get("consolidated") == basis
                and r.get("type_Sub") == "Revision"
                and r.get("xbrl")]
        orig_xml = download_xbrl(row["xbrl"])
        for rev in revs:
            if download_xbrl(rev["xbrl"]) != orig_xml:
                return row, None, "revision-differs"
        return row, orig_xml, None
    return row, download_xbrl(row["xbrl"]), None


def try_pdf_parse(pdf_url_):
    """Best-effort PDF parse for the disagreement check, or None.

    Re-downloads the filing's PDF and runs the embedded+layout
    parse chain (mirroring process_item, minus OCR: OCR already
    ran at original post time and failed, and re-running it per
    XBRL attempt would cost minutes per entry). None means "no
    PDF parse available" — download failure, invalid PDF, or
    unparseable text — in which case the XBRL build stands
    alone and no disagreement check is possible.
    """
    try:
        r = requests.get(pdf_url_, headers=HEADERS, timeout=60)
        r.raise_for_status()
        pdf = r.content
    except Exception:
        return None
    if not pdf_looks_valid(pdf):
        return None
    parsed = None
    text = None
    try:
        text = extract_text(pdf)
    except Exception:
        text = None
    if text is not None:
        try:
            parsed = parse_results(text)
        except Exception:
            parsed = None
        if parsed is None or parsed.get("basis") == "Standalone":
            try:
                layout = extract_layout_text(pdf)
            except Exception:
                layout = None
            if layout:
                try:
                    retry = parse_results(text, layout_text=layout)
                except Exception:
                    retry = None
                if retry is not None and retry.get("basis") == "Consolidated":
                    parsed = retry
    return parsed


def _pct_change(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return (cur / base - 1.0) * 100.0


def _pct_signed_change(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return (cur - base) / abs(base) * 100.0


def adopt_pdf_comparatives(xbrl_parsed, pdf_parsed):
    """XBRL card with the PDF's printed comparatives, recomputed.

    Only the comparative columns (previous, year-ago) are taken
    from the PDF; the current-quarter triple stays 100% XBRL.
    A PDF comparative is adopted only when it is non-None, so a
    blanked PDF value never erases an XBRL one. All derived
    fields (QoQ/YoY, PAT Margin, rating, reason) are recomputed
    from the merged triple — the XBRL rating is never kept
    alongside PDF comparatives.
    """
    out = dict(xbrl_parsed)
    for key in ("sales", "other", "total", "pat", "eps"):
        xv = xbrl_parsed.get(key)
        pv = pdf_parsed.get(key)
        if xv is None or pv is None:
            continue
        out[key] = (xv[0],
                    pv[1] if pv[1] is not None else xv[1],
                    pv[2] if pv[2] is not None else xv[2])
    s, sp, sl = out["sales"]
    o, op, ol = out["other"]
    t, tp, tl = out["total"]
    p, pp, pl = out["pat"]
    e = out["eps"] or (None, None, None)
    out["sales_qoq"] = _pct_change(s, sp)
    out["sales_yoy"] = _pct_change(s, sl)
    out["pat_qoq"] = _pct_signed_change(p, pp)
    out["pat_yoy"] = _pct_signed_change(p, pl)
    out["total_qoq"] = _pct_change(t, tp)
    out["total_yoy"] = _pct_change(t, tl)
    out["eps_qoq"] = _pct_change(e[0], e[1]) if out["eps"] else None
    out["eps_yoy"] = _pct_change(e[0], e[2]) if out["eps"] else None

    def margin(pat_v, sales_v):
        return (pat_v / sales_v * 100.0) if sales_v else None

    out["pat_margin"] = (margin(p, s),
                         margin(pp, sp) if sp else None,
                         margin(pl, sl) if sl else None)
    rating, reason = verdict(out["sales_yoy"], out["pat_yoy"])
    out["rating"] = rating
    out["reason"] = reason
    return out


def xbrl_disagrees(xbrl_parsed, pdf_parsed):
    """(True, detail) when the current-quarter values conflict.

    Compared only where BOTH sources have a value: a blanked PDF
    metric (e.g. EPS blanked by its ratio guard) is not a value
    and cannot disagree. Money tolerance 0.05 Cr, EPS 0.01 —
    XBRL facts are exact while PDF tables are rounded, so a
    smaller tolerance would refuse on rounding alone.
    """
    for key in ("sales", "other", "total", "pat"):
        a = xbrl_parsed[key][0]
        b = pdf_parsed[key][0]
        if a is not None and b is not None and abs(a - b) > XBRL_MONEY_TOL:
            return True, f"{key} XBRL {a} vs PDF {b}"
    ea = (xbrl_parsed["eps"] or (None, None, None))[0]
    eb = (pdf_parsed["eps"] or (None, None, None))[0]
    if ea is not None and eb is not None and abs(ea - eb) > XBRL_EPS_TOL:
        return True, f"eps XBRL {ea} vs PDF {eb}"
    return False, ""


def attempt_xbrl_recovery(entry, token, chat_id, state):
    """Try to recover one pending entry from its XBRL document.

    Returns "posted" (card sent, caller removes the entry),
    "already-card" (a card already exists for this filing, caller
    removes the stale entry), "pending" (tried, keep retrying
    under throttle until expiry), or "blocked" (a permanent
    refusal — revision conflict, PDF disagreement, or a
    taxonomy this code does not map — recorded on the entry so
    future runs skip it without further API calls).
    Network/parse exceptions propagate to the caller, which
    treats them as "pending" (a transient failure is never a
    permanent refusal).

    IRON DISAGREEMENT RULE (XBRL Phase 1):
    NEVER blend sources within one card's current-quarter triple
    (all five metrics from one source). PDF comparatives (QoQ/YoY
    columns as printed in the current filing) always win over
    XBRL-derived comparatives. An XBRL build whose current-quarter
    values disagree with an available PDF parse beyond 0.05 Cr
    (money) / 0.01 (EPS) is REFUSED for that filing (log the
    disagreement, keep the existing post standing). On
    disagreement, NEVER guess: fall back to safe text/screenshot,
    or keep the already-posted form standing — never post a
    second, conflicting card.
    """
    symbol = entry.get("symbol") or "?"
    company = entry.get("company") or symbol
    pdf_url_ = entry.get("url") or ""
    an_dt = entry.get("an_dt") or ""
    ukey = filing_url_key(pdf_url_)
    cards, _ = archive_url_keys(state)
    if ukey and ukey in cards:
        print(f"[{symbol}] XBRL: card already posted; dropping entry")
        return "already-card"
    an_date = parse_an_dt_date(an_dt)
    if an_date is None:
        an_date = datetime.strptime(ist_today(), "%Y-%m-%d").date()
    from_d = (an_date - timedelta(days=7)).strftime("%d-%m-%Y")
    to_d = (an_date + timedelta(days=7)).strftime("%d-%m-%Y")
    rows = _integrated_rows(symbol, from_d, to_d)
    row, basis = pick_xbrl_row_for_entry(rows, entry)
    if row is None:
        print(f"[{symbol}] XBRL: no row yet for this filing; keep pending")
        _stamp_xbrl_try(entry)
        return "pending"
    qe_api = row.get("qe_Date")
    qe_iso = qe_api_to_iso(qe_api)
    if qe_iso is None:
        print(f"[{symbol}] XBRL: unparseable qe_Date {qe_api!r}")
        _stamp_xbrl_try(entry)
        return "pending"
    row, xml, refusal = resolve_xbrl_row(rows, row, basis)
    if refusal == "revision-differs":
        print(f"[{symbol}] XBRL: Original and Revision differ; refused")
        entry["xbrl_blocked"] = "revision-differs"
        _stamp_xbrl_try(entry)
        return "blocked"
    tax = detect_taxonomy(xml)
    if tax in ("banking", "other"):
        print(f"[{symbol}] XBRL: {tax} taxonomy refused, never guessed")
        entry["xbrl_blocked"] = f"{tax}-taxonomy"
        _stamp_xbrl_try(entry)
        return "blocked"
    if tax is None:
        print(f"[{symbol}] XBRL: unparseable XML; keep pending")
        _stamp_xbrl_try(entry)
        return "pending"
    # Prior-year document (same basis): REQUIRED for a real
    # rating — without YoY, verdict() returns "Results Filed",
    # which must never be posted as a card. Best-effort only
    # in the sense that a missing prior-year row keeps the
    # entry pending (the document is old; it should exist).
    py_iso = prior_year_iso(qe_iso)
    py_xml = None
    if py_iso is not None:
        py_date = datetime.strptime(py_iso, "%Y-%m-%d").date()
        py_rows = _integrated_rows(
            symbol, py_date.strftime("%d-%m-%Y"),
            (py_date + timedelta(days=60)).strftime("%d-%m-%Y"))
        py_row = select_xbrl_row(py_rows, iso_to_qe_api(py_iso),
                                 basis)
        if py_row is not None:
            py_xml = download_xbrl(py_row["xbrl"])
    # Previous-quarter document (best effort, non-Q2 only):
    # Q2 derives the previous quarter exactly as HY - Q.
    prev_xml = None
    ql = quarter_label_from_iso(qe_iso)
    if ql is not None and not ql.startswith("Q2"):
        prev_iso = prev_quarter_end_iso(qe_iso)
        if prev_iso is not None:
            prev_date = datetime.strptime(prev_iso, "%Y-%m-%d").date()
            prev_rows = _integrated_rows(
                symbol, prev_date.strftime("%d-%m-%Y"),
                (prev_date + timedelta(days=60)).strftime("%d-%m-%Y"))
            prev_row = select_xbrl_row(prev_rows,
                                       iso_to_qe_api(prev_iso), basis)
            if prev_row is not None:
                prev_xml = download_xbrl(prev_row["xbrl"])
    parsed = xbrl_build_card(xml, qe_iso, basis,
                             prior_year_xml=py_xml,
                             prev_quarter_xml=prev_xml)
    if parsed is None:
        print(f"[{symbol}] XBRL: build refused "
              f"({xbrl_parser.LAST_REFUSAL}); keep pending")
        _stamp_xbrl_try(entry)
        return "pending"
    if parsed["rating"] == "Results Filed":
        print(f"[{symbol}] XBRL: no YoY rating (prior-year doc missing); "
              "keep pending")
        _stamp_xbrl_try(entry)
        return "pending"
    pdf_parsed = try_pdf_parse(pdf_url_)
    if pdf_parsed is not None:
        bad, detail = xbrl_disagrees(parsed, pdf_parsed)
        if bad:
            print(f"[{symbol}] XBRL: PDF disagreement ({detail}); refused, "
                  "existing post stands")
            entry["xbrl_blocked"] = "pdf-xbrl-disagreement"
            _stamp_xbrl_try(entry)
            return "blocked"
        if pdf_parsed.get("basis") == basis:
            parsed = adopt_pdf_comparatives(parsed, pdf_parsed)
            if parsed["rating"] == "Results Filed":
                print(f"[{symbol}] XBRL: adoption lost the rating; "
                      "keep pending")
                _stamp_xbrl_try(entry)
                return "pending"
    remote_cards = remote_card_url_keys()
    if ukey and remote_cards is not None and ukey in remote_cards:
        print(f"[{symbol}] XBRL: remote card already posted; dropping entry")
        return "already-card"
    q_labels = quarter_month_labels(parsed["quarter_label"])
    with tempfile.TemporaryDirectory() as td:
        data = card_data_from_parse(company, symbol, parsed, an_dt,
                                    q_labels)
        img = os.path.join(td, f"{symbol}.png")
        render_card(data, img)
        send_photo(token, chat_id, img,
                   build_caption(symbol, company, parsed, pdf_url_),
                   pdf_url_, symbol)
    print(f"[{symbol}] XBRL recovered {parsed['rating']} card "
          f"({basis}, {parsed['quarter_label']})")
    record_archive(state, symbol, company, parsed["rating"],
                   parsed.get("basis", ""), parsed["quarter_label"],
                   parsed["sales"][0], parsed["pat"][0],
                   (parsed["eps"] or (None, None, None))[0],
                   pdf_url_, an_dt, xbrl=row["xbrl"])
    return "posted"


def maybe_xbrl_retry(token, chat_id, state, force=False):
    """Retry pending text_posts entries from XBRL (see module doc).

    Returns the set of entry keys recovered (card posted, or a
    card already existed) — the caller removes those entries.
    Entries are skipped without any network call when blocked,
    date-stale, expired (>7 days), or throttled (normal runs
    only; force=True bypasses the throttle for the nightly
    pass). State is saved once at the end when anything changed
    (a recovery, a removal, or a try-stamp). Never raises: a
    per-entry failure is logged and left pending, and a bug in
    this gate must never break results posting.
    """
    try:
        dg = state.get("digest", {})
        entries = list(dg.get("text_posts", []))
        if not entries:
            return set()
        now = ist_now()
        today_str = ist_today()
        cards, _ = archive_url_keys(state)
        recovered = set()
        changed = False
        for e in entries:
            key = e.get("key")
            if key in recovered:
                continue
            if e.get("xbrl_blocked"):
                continue
            ukey = filing_url_key(e.get("url") or "")
            if ukey and ukey in cards:
                recovered.add(key)
                changed = True
                continue
            if xbrl_entry_date_stale(e, today_str):
                continue
            if xbrl_entry_age_days(e) > MAX_AGE_DAYS:
                continue
            if not force and not xbrl_entry_due(e, now):
                continue
            try:
                status = attempt_xbrl_recovery(e, token, chat_id, state)
            except Exception as exc:
                print(f"[{e.get('symbol')}] XBRL recovery failed ({exc})")
                _stamp_xbrl_try(e)
                changed = True
                continue
            if status in ("posted", "already-card"):
                recovered.add(key)
                changed = True
            else:
                changed = True
        if recovered:
            dg["text_posts"] = [e for e in entries
                                if e.get("key") not in recovered]
        if changed:
            save_state(state)
        return recovered
    except Exception as exc:
        print(f"[xbrl] retry gate failed ({exc})")
        return set()

# ================= end XBRL Phase 1 recovery =================


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
    # XBRL recovery (forced: the nightly pass ignores the hourly
    # throttle) for every still-pending entry, not just today's:
    # a filing whose XBRL landed late is recovered here even if
    # its PDF retry already ran earlier in the day.
    try:
        posted |= maybe_xbrl_retry(token, chat_id, state, force=True)
    except Exception as exc:
        print(f"[nightly] XBRL retry skipped ({exc})")
    # Drop retried-as-cards entries and entries older than the
    # 7-day retry window. Entries from earlier in the window are
    # KEPT (previous behaviour dropped every non-today entry,
    # which would have killed the XBRL retry window for filings
    # that failed on earlier days).
    def _keep(e):
        if e.get("key") in posted:
            return False
        try:
            d = datetime.strptime(e.get("date") or "",
                                  "%Y-%m-%d").date()
            today_d = datetime.strptime(today, "%Y-%m-%d").date()
            return 0 <= (today_d - d).days <= MAX_AGE_DAYS
        except ValueError:
            return False
    dg["text_posts"] = [e for e in dg.get("text_posts", []) if _keep(e)]
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
    # Queue/overlap guard: pick up any state pushed by a previous
    # run after this run's checkout, before deciding what is new.
    resync_state_from_git(state)
    seen = set(state.get("seen", []))
    cards, placeholders = archive_url_keys(state)
    already_posted = cards | placeholders
    new = [it for it in results
           if filing_key(it) not in seen
           and filing_url_key(pdf_url(it)) not in already_posted]
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
        # not post again from the feed pass (or vice versa) — by
        # key AND by filing URL, so the same filing under a
        # different key cannot slip through either.
        if filing_key(it) in state["seen"]:
            continue
        _c, _p = archive_url_keys(state)
        if filing_url_key(pdf_url(it)) in (_c | _p):
            continue
        if filing_age_days(it) > MAX_AGE_DAYS:
            print(f"[{it.get('symbol')}] older than {MAX_AGE_DAYS}d, skipping")
            state["seen"].append(filing_key(it))
            save_state(state)
            continue
        process_item(it, token, chat_id, state)
    try:
        # XBRL recovery for filings that posted as text/screenshot
        # on earlier runs (throttled; a failure here never breaks
        # posting — maybe_xbrl_retry itself never raises).
        maybe_xbrl_retry(token, chat_id, state)
    except Exception as exc:
        print(f"[xbrl] retry skipped ({exc})")
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
