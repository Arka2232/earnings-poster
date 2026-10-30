"""Scheduled digest posts for Share Market Charts (morning calendar,
daily market wrap, weekly corporate-actions digest, IPO section).

Runs inside the same 5-minute poster workflow: watcher.py calls
maybe_run_digests() after the results filings, guarded so a digest
failure can never break results posting.

Every digest post is an IMAGE + a short text caption (names only,
no company logos anywhere — standing rule, 31-Oct-2026), so the
facts stay searchable while the image carries the full detail.

Sources (all free, no key; same header pattern as the poster —
browser User-Agent + Referer, with retries because NSE 403s
intermittently):
- Results calendar + action meetings: /api/event-calendar?index=equities
  (forward-looking board-meeting dates; past dates drop off, so the
  morning fetch sees today's rows).
- IPOs: /api/ipo-current-issue (open/close dates, price band,
  subscription multiple; NO lot size or listing date — v1 omits them).
- Wrap: /api/allIndices (NIFTY closes, breadth, sector moves) +
  /api/fiidiiTradeReact (official FII/DII provisional figures).

Honest limits baked into the posts:
- The actions digest lists board MEETINGS to consider dividend /
  bonus / buyback — it is NOT an ex-date calendar (no verified NSE
  endpoint exposes ex-dates).
- Sensex is omitted (BSE API blocked; mirrors disagree). The wrap
  leads with NIFTY. No individual gainers/losers (no verified
  endpoint); the wrap shows sector movers instead.
- GMP comes from gmptoday.in (median of public trackers, CC-BY-4.0,
  attribution on the card) and is always labelled unofficial and
  unregulated — never presented as exchange data.
- Every post ends with the education-only disclaimer.
"""

import html
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests

from telegram_post import send_photo_plain, send_text
from digest_images import (render_actions_image, render_fiidii_image,
                           render_ipo_image, render_morning_image,
                           render_recap_image, render_wrap_image)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}
EVENTS_URL = "https://www.nseindia.com/api/event-calendar?index=equities"
IPO_URL = "https://www.nseindia.com/api/ipo-current-issue"
INDICES_URL = "https://www.nseindia.com/api/allIndices"
FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
GMP_URL = "https://gmptoday.in/api/gmp.json"

DISCLAIMER = ("For education only — not investment advice. "
              "I am not SEBI-registered.")
IPO_DISCLAIMER = ("For education only — not investment advice. "
                  "I am not SEBI-registered. "
                  "GMP is unofficial and unregulated.")

SECTORS = ["NIFTY BANK", "NIFTY IT", "NIFTY PHARMA", "NIFTY AUTO",
           "NIFTY METAL", "NIFTY FMCG", "NIFTY ENERGY", "NIFTY REALTY",
           "NIFTY MEDIA", "NIFTY PSU BANK", "NIFTY PVT BANK",
           "NIFTY HEALTHCARE"]


def ist_now():
    """Current time in IST, without depending on tzdata being present."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def fetch_nse_json(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data.get("data") or []
            return data
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise RuntimeError(f"NSE fetch failed ({url}): {last}")


def parse_nse_date(raw):
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").date()
    except (ValueError, AttributeError):
        return None


def fmt_date(d):
    return d.strftime("%d %b %Y")


def fmt_cr(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "–"
    sign = "-" if v < 0 else ""
    return f"{sign}₹{abs(v):,.0f} Cr"


def send_long_text(token, chat_id, text):
    """Send a post, split on line boundaries if it exceeds Telegram's
    4096-character message limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000 and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    for i, chunk in enumerate(chunks):
        prefix = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
        send_text(token, chat_id, prefix + chunk)


def _is_results_purpose(purpose):
    return "financial results" in (purpose or "").lower()


def _is_action_purpose(purpose):
    p = (purpose or "").lower()
    return "dividend" in p or "bonus" in p or "buyback" in p


def _morning_parts(today, events, ipos):
    """Upcoming-days lines and open-IPO lines shared by the morning
    text post and the morning image caption."""
    upcoming = []
    for offset in (1, 2, 3):
        day = today + timedelta(days=offset)
        n = sum(1 for e in events
                if parse_nse_date(e.get("date")) == day
                and _is_results_purpose(e.get("purpose")))
        if n:
            upcoming.append(f"{day.strftime('%d %b')} — {n} companies")
    ipo_lines = []
    active = [i for i in ipos if (i.get("status") or "") == "Active"]
    for ipo in active:
        end = parse_nse_date(ipo.get("issueEndDate"))
        price = ipo.get("issuePrice") or "Price not disclosed"
        try:
            sub = float(ipo.get("noOfTime"))
            sub_txt = f"subscribed {sub:.1f}x"
        except (TypeError, ValueError):
            sub_txt = ""
        closing = ""
        if end and (end - today).days <= 3:
            closing = " ⏰ Closing soon"
        ipo_lines.append(
            f"{ipo.get('companyName')} ({ipo.get('symbol')}) — "
            f"{ipo.get('issueStartDate')} to {ipo.get('issueEndDate')} — "
            f"{price} — {sub_txt}{closing}".strip())
    return upcoming, ipo_lines


def build_morning_post(today, events, ipos, today_rows=None):
    """Today's results + next few days + open IPOs.

    today_rows overrides the live computation: the event-calendar
    drops today's rows after midnight IST, so the morning gate
    passes the previous evening's cached snapshot instead."""
    if today_rows is None:
        today_rows = sorted(
            (e.get("symbol") for e in events
             if parse_nse_date(e.get("date")) == today
             and _is_results_purpose(e.get("purpose"))))
    else:
        today_rows = sorted(set(today_rows))
    upcoming, ipo_lines = _morning_parts(today, events, ipos)
    lines = [f"📅 Results Calendar — {fmt_date(today)}", ""]
    if today_rows:
        lines.append(f"Today's results ({len(today_rows)}):")
        lines.append(", ".join(today_rows))
    else:
        lines.append("No results scheduled for today.")
    lines.append("")
    if upcoming:
        lines.append("Coming up:")
        lines.extend(upcoming)
        lines.append("")
    if ipo_lines:
        lines.append("📈 Open IPOs:")
        lines.extend(ipo_lines)
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def build_wrap_post(today, indices, fiidii):
    """NIFTY closes, breadth, sector movers, official FII/DII."""
    by_name = {row.get("index"): row for row in indices}
    nifty = by_name.get("NIFTY 50") or {}
    breadth = by_name.get("NIFTY 500") or {}
    lines = [f"📊 Market Wrap — {fmt_date(today)}", ""]
    if nifty:
        chg = nifty.get("variation")
        pct = nifty.get("percentChange")
        lines.append(f"NIFTY 50: {nifty.get('last')} "
                     f"({chg:+,.2f}, {pct:+.2f}%)" if chg is not None
                     else f"NIFTY 50: {nifty.get('last')}")
    adv, dec = breadth.get("advances"), breadth.get("declines")
    if adv is not None and dec is not None:
        lines.append(f"NIFTY 500 breadth: {adv} advances, {dec} declines")
    sectors = [(row.get("index"), row.get("percentChange"))
               for row in indices
               if row.get("index") in SECTORS
               and isinstance(row.get("percentChange"), (int, float))]
    if sectors:
        ups = [s for s in sorted(sectors, key=lambda s: s[1], reverse=True)
               if s[1] > 0][:3]
        downs = [s for s in sorted(sectors, key=lambda s: s[1])
                 if s[1] < 0][:3]
        if ups or downs:
            lines.append("")
        if ups:
            lines.append("Sectors up: " + ", ".join(
                f"{n.replace('NIFTY ', '')} {p:+.2f}%" for n, p in ups))
        if downs:
            lines.append("Sectors down: " + ", ".join(
                f"{n.replace('NIFTY ', '')} {p:+.2f}%" for n, p in downs))
    if fiidii:
        lines.append("")
        lines.append("FII / DII (provisional, cash market):")
        for row in fiidii:
            cat = row.get("category")
            lines.append(f"{cat}: net {fmt_cr(row.get('netValue'))} "
                         f"(buy {fmt_cr(row.get('buyValue'))} / "
                         f"sell {fmt_cr(row.get('sellValue'))})")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _actions_by_day(today, events):
    """(day label, symbols) for the next 7 days' action meetings."""
    out = []
    for i in range(7):
        day = today + timedelta(days=i)
        syms = sorted(e.get("symbol") for e in events
                      if parse_nse_date(e.get("date")) == day
                      and _is_action_purpose(e.get("purpose")))
        if syms:
            out.append((day.strftime("%d %b"), syms))
    return out


def build_actions_post(today, events):
    """Upcoming board meetings to consider dividend/bonus/buyback.

    Returns None when there is nothing in the next 7 days (the week
    is then skipped silently). These are MEETING dates, not ex-dates.
    """
    by_day = _actions_by_day(today, events)
    if not by_day:
        return None
    lines = ["💰 Corporate Actions — meetings this week", "",
             "Board meetings to consider dividend / bonus / buyback. "
             "These are meeting dates, not ex-dates.", ""]
    for label, syms in by_day:
        lines.append(f"{label}: " + ", ".join(syms))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _in_window(now, start_h, start_m, end_h, end_m):
    t = now.hour * 60 + now.minute
    return start_h * 60 + start_m <= t < end_h * 60 + end_m


def fetch_indices(tries=3):
    """allIndices rows + the top-level timestamp ("30-Oct-2026
    15:30"). The timestamp is the traded-day proof: on a holiday
    the rows would be stale, but the timestamp would not be today."""
    last = None
    for _ in range(tries):
        try:
            r = requests.get(INDICES_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            rows = data.get("data") or []
            return rows, data.get("timestamp") or ""
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise RuntimeError(f"NSE indices fetch failed: {last}")


def fetch_gmp(tries=2):
    """gmptoday.in median-GMP list (CC-BY-4.0). Returns [] on any
    failure — a failed GMP fetch postpones IPO cards, it never
    invents a GMP."""
    for _ in range(tries):
        try:
            r = requests.get(GMP_URL, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data.get("ipos") or []
            return []
        except Exception:
            time.sleep(2)
    return []


def _during_market(an_dt):
    """True when an NSE filing timestamp falls inside market hours
    (09:15–15:30 IST). Unparseable timestamps fall back to After."""
    try:
        dt = datetime.strptime((an_dt or "").strip(),
                               "%d-%b-%Y %H:%M:%S")
        return (9, 15) <= (dt.hour, dt.minute) <= (15, 30)
    except (ValueError, AttributeError):
        return False


def _today_results_symbols(today, events, state):
    """Today's results symbols. The event-calendar drops today's
    rows after midnight IST, so when the live fetch has none, fall
    back to the previous evening's cached snapshot — but only when
    the snapshot is dated today, so a stale list can never leak
    into another day."""
    live = sorted({e.get("symbol") for e in events
                   if parse_nse_date(e.get("date")) == today
                   and _is_results_purpose(e.get("purpose"))})
    if live:
        return live
    cache = (state.get("digest") or {}).get("next_results") or {}
    if cache.get("date") == today.isoformat():
        return sorted(set(cache.get("symbols") or []))
    return []


def _post_image(token, chat_id, render_fn, caption):
    """Render one digest image to a temp file and send it with a
    caption and no buttons."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "digest.png")
        render_fn(path)
        send_photo_plain(token, chat_id, path, caption)


def _norm_name(name):
    """Normalize a company name for IPO/GMP matching. Suffixes and
    the word "and" are dropped, because the same company appears as
    "Bombay Coated and Special Steels Limited" (NSE) and "Bombay
    Coated & Special Steels" (tracker) — without this, the GMP match
    silently fails and the card posts without a GMP row."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(limited|ltd|private|pvt|and)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _match_gmp(company, gmp_rows):
    cn = _norm_name(company)
    for g in gmp_rows:
        gn = _norm_name(g.get("name"))
        if gn and (gn == cn or cn.startswith(gn) or gn.startswith(cn)):
            return g
    return None


def _strip_html(page):
    """Strip fetched HTML down to visible text for table parsing."""
    t = re.sub(r"<script.*?</script>", " ", page, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = html.unescape(t)
    return re.sub(r"\|+", " | ", t)


def _cr(amount):
    """Format a rupee-crore figure: whole numbers without decimals."""
    try:
        x = float(amount)
    except (TypeError, ValueError):
        return None
    if x == int(x):
        return f"₹{x:,.0f} Cr"
    return f"₹{x:,.2f} Cr"


def _chg_slug_variants(company):
    """Chittorgarh detail-page slugs for a company name. The site
    redirects a slug-only URL to the full slug+id URL, which is
    what makes discovery work without knowing the numeric id."""
    name = (company or "").lower().replace("&", " and ")
    name = re.sub(r"\b(limited|ltd|private|pvt|company|co)\b", " ",
                  name)
    slug = re.sub(r"-+", "-",
                  re.sub(r"[^a-z0-9]+", "-", name)).strip("-")
    variants = [slug]
    no_and = slug.replace("-and-", "-").strip("-")
    if no_and and no_and != slug:
        variants.append(no_and)
    return [v for v in variants if v]


def _chg_shares_after(text, label, window=120):
    """First '<number> shares' after a label, else None."""
    i = text.find(label)
    if i < 0:
        return None
    m = re.search(r"([\d,]+)(?:\s*\|)*\s*shares", text[i:i + window])
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _chg_mm_shares(text):
    """Market-maker share count: the 'Market Maker' occurrence
    whose following text starts with a number (the name row, e.g.
    'Hem Finlease', is skipped). None when there is no such row."""
    for m in re.finditer(r"Market Maker", text):
        seg = text[m.end():m.end() + 40]
        n = re.match(r"\s*\|?\s*([\d,]{4,})", seg)
        if n:
            try:
                return int(n.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _chg_parse_split(text):
    """Parse one chittorgarh details-table window. The split is
    accepted only when its parts sum EXACTLY to the total share
    count (fresh ex-market-maker + market maker + OFS == total);
    rupee figures are shares x upper band from the same page.
    Returns the split dict or None."""
    for anchor in re.finditer(r"Total Issue Size", text):
        win = text[anchor.start():anchor.start() + 1500]
        total = _chg_shares_after(win, "Total Issue Size")
        fresh = _chg_shares_after(win, "Fresh Issue")
        if total is None or fresh is None:
            continue
        mm = _chg_mm_shares(win) or 0
        ofs = _chg_shares_after(win, "Offer for Sale")
        if ofs is None:
            seg = ""
            j = win.find("Offer for Sale")
            if j >= 0:
                seg = win[j:j + 80]
            if ("Not Applicable" in seg or "not applicable" in seg
                    or fresh + mm == total):
                ofs = 0
            else:
                continue
        if fresh + mm + ofs != total:
            continue
        band = re.search(r"Price Band(?:\s*\|)*\s*₹\s*([\d,]+)\s*to\s*"
                         r"₹\s*([\d,]+)", text)
        lot = re.search(r"Lot Size(?:\s*\|)*\s*([\d,]+)(?:\s*\|)*\s*"
                        r"Shares", text)
        if not band:
            continue
        try:
            low = float(band.group(1).replace(",", ""))
            high = float(band.group(2).replace(",", ""))
        except ValueError:
            continue
        if high <= 0:
            continue
        fresh_total = fresh + mm
        return {
            "total": _cr(total * high / 1e7),
            "fresh": _cr(fresh_total * high / 1e7),
            "ofs": _cr(ofs * high / 1e7) if ofs else None,
            "price_low": low,
            "price_high": high,
            "lot": f"{lot.group(1)} shares" if lot else None,
        }
    return None


def fetch_chittorgarh_split(company):
    """Fresh-issue / OFS split from chittorgarh.com's IPO detail
    page. Returns None on ANY failure — a failed fetch, a page
    that is not this company's, or a failed consistency check
    omits the split rows, it never invents them. Never raises."""
    try:
        words = [w for w in re.split(r"[^A-Za-z0-9]+",
                                     company or "")
                 if w.lower() not in ("limited", "ltd", "private",
                                      "pvt", "company", "co")]
        check = [w.lower() for w in words[:2]]
        if len(check) < 2:
            return None
        headers = {"User-Agent": HEADERS["User-Agent"]}
        for slug in _chg_slug_variants(company):
            try:
                r = requests.get(
                    f"https://www.chittorgarh.com/ipo/{slug}-ipo/",
                    headers=headers, timeout=20)
                if r.status_code != 200:
                    continue
                text = _strip_html(r.text)
                if not all(w in text.lower() for w in check):
                    continue
                split = _chg_parse_split(text)
                if split:
                    return split
            except Exception:
                continue
        return None
    except Exception:
        return None


def _upper_price(text):
    try:
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?",
                                             text or "")]
        return max(nums) if nums else None
    except (TypeError, ValueError):
        return None


def _fmt_iso_date(raw):
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").strftime(
            "%d %b %Y")
    except (ValueError, AttributeError):
        return None


def _morning_caption(today, today_rows, events, ipos):
    upcoming, ipo_lines = _morning_parts(today, events, ipos)
    lines = [f"📅 Results Calendar — {fmt_date(today)}",
             f"Today's results ({len(today_rows)}): "
             "full list on the image."]
    if upcoming:
        lines.append("")
        lines.append("Coming up:")
        lines.extend(upcoming)
    if ipo_lines:
        lines.append("")
        lines.append("📈 Open IPOs:")
        lines.extend(ipo_lines)
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _recap_caption(today, by_rating):
    total = 0
    lines = [f"📊 Earnings Recap — {fmt_date(today)}"]
    for rating in ["Excellent", "Good", "Positive", "OK", "Weak",
                   "Bad", "Filed"]:
        names = sorted(set((by_rating.get(rating) or ([], []))[0]
                            + (by_rating.get(rating) or ([], []))[1]))
        if names:
            total += len(names)
            lines.append(f"{rating}: {', '.join(names)}")
    lines[0] = f"📊 Earnings Recap — {fmt_date(today)} ({total} results)"
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def maybe_run_digests(token, chat_id, state, save_fn, now=None):
    """Run whichever digest gates are open. Flags in state["digest"]
    make each digest fire once per period; a failed fetch leaves the
    flag unset so the next 5-minute run retries inside the window.

    Gates (all IST):
    - Evening cache 22:00–22:30: snapshot tomorrow's results list,
      because the event-calendar drops today's rows after midnight.
    - Morning 08:00–08:30: Today Earnings image + caption (text only
      on a genuinely empty day).
    - Wrap + FII/DII 19:30–20:00 daily: post ONLY when the day's own
      market data is present (NIFTY timestamp dated today AND
      FII/DII dated today) — this is the traded-day rule, not
      weekday(), so holidays skip and special Sunday sessions post.
    - Recap 22:30–23:00: the day's archive entries grouped by rating
      × During/After Market; unparsed filings under "Filed".
    - Actions Sunday 10:00–10:30: meetings-this-week image + caption.
    - IPO check hourly: one card per newly Active IPO (NSE facts +
      gmptoday.in GMP, labelled unofficial)."""
    now = now or ist_now()
    today = now.date()
    dg = state.setdefault("digest", {})

    if (_in_window(now, 22, 0, 22, 30)
            and dg.get("next_cache") != today.isoformat()):
        try:
            events = fetch_nse_json(EVENTS_URL)
            tomorrow = today + timedelta(days=1)
            syms = sorted({e.get("symbol") for e in events
                           if parse_nse_date(e.get("date")) == tomorrow
                           and _is_results_purpose(e.get("purpose"))})
            dg["next_results"] = {"date": tomorrow.isoformat(),
                                  "symbols": syms}
            dg["next_cache"] = today.isoformat()
            save_fn(state)
            print(f"[digest] cached {len(syms)} results for {tomorrow}")
        except Exception as exc:
            print(f"[digest] evening cache failed ({exc})")

    if (_in_window(now, 8, 0, 8, 30)
            and dg.get("calendar") != today.isoformat()):
        try:
            events = fetch_nse_json(EVENTS_URL)
            ipos = fetch_nse_json(IPO_URL)
            today_rows = _today_results_symbols(today, events, state)
            if today_rows:
                _post_image(
                    token, chat_id,
                    lambda p: render_morning_image(
                        fmt_date(today), today_rows, p),
                    _morning_caption(today, today_rows, events, ipos))
            else:
                send_long_text(
                    token, chat_id,
                    build_morning_post(today, events, ipos,
                                       today_rows=[]))
            dg["calendar"] = today.isoformat()
            save_fn(state)
            print(f"[digest] morning calendar posted for {today} "
                  f"({len(today_rows)} results)")
        except Exception as exc:
            print(f"[digest] morning calendar failed ({exc})")

    if (_in_window(now, 19, 30, 20, 0)
            and dg.get("wrap") != today.isoformat()):
        try:
            indices, ts = fetch_indices()
            fiidii = fetch_nse_json(FIIDII_URL)
            ts_date = parse_nse_date((ts or "").split(" ")[0])
            fii_today = any(parse_nse_date(r.get("date")) == today
                            for r in fiidii)
            if ts_date != today or not fii_today:
                print(f"[digest] no traded-day data for {today}; "
                      "wrap skipped")
            else:
                by_name = {row.get("index"): row for row in indices}
                nifty = by_name.get("NIFTY 50") or {}
                breadth = by_name.get("NIFTY 500") or {}
                sectors = [
                    (row.get("index").replace("NIFTY ", ""),
                     row.get("percentChange"))
                    for row in indices
                    if row.get("index") in SECTORS
                    and isinstance(row.get("percentChange"),
                                   (int, float))]
                ups = [s for s in sorted(sectors, key=lambda s: s[1],
                                         reverse=True) if s[1] > 0][:3]
                downs = [s for s in sorted(sectors, key=lambda s: s[1])
                         if s[1] < 0][:3]
                fii = {r.get("category"): r for r in fiidii}
                fii_row = fii.get("FII/FPI") or {}
                dii_row = fii.get("DII") or {}

                def _f(row, key):
                    try:
                        return float(row.get(key))
                    except (TypeError, ValueError):
                        return 0.0

                fii_net, dii_net = (_f(fii_row, "netValue"),
                                    _f(dii_row, "netValue"))
                nifty_last = _f(nifty, "last")
                nifty_chg = _f(nifty, "variation")
                nifty_pct = _f(nifty, "percentChange")
                try:
                    adv = int(breadth.get("advances"))
                    dec = int(breadth.get("declines"))
                except (TypeError, ValueError):
                    adv, dec = 0, 0
                _post_image(
                    token, chat_id,
                    lambda p: render_wrap_image(
                        fmt_date(today),
                        (nifty_last, nifty_chg, nifty_pct), (adv, dec),
                        ups, downs, fmt_cr(fii_net), fmt_cr(dii_net), p),
                    f"📊 Market Wrap — {fmt_date(today)}\n"
                    f"NIFTY 50: {nifty_last:,.2f} "
                    f"({nifty_chg:+,.2f}, {nifty_pct:+.2f}%)\n"
                    f"FII net {fmt_cr(fii_net)} • "
                    f"DII net {fmt_cr(dii_net)} (provisional)\n\n"
                    f"{DISCLAIMER}")
                _post_image(
                    token, chat_id,
                    lambda p: render_fiidii_image(
                        fmt_date(today),
                        (_f(fii_row, "buyValue"),
                         _f(fii_row, "sellValue"), fii_net),
                        (_f(dii_row, "buyValue"),
                         _f(dii_row, "sellValue"), dii_net), p),
                    f"FII / DII — {fmt_date(today)} "
                    "(provisional, cash market)\n"
                    f"FII: net {fmt_cr(fii_net)} "
                    f"(buy {fmt_cr(_f(fii_row, 'buyValue'))} / "
                    f"sell {fmt_cr(_f(fii_row, 'sellValue'))})\n"
                    f"DII: net {fmt_cr(dii_net)} "
                    f"(buy {fmt_cr(_f(dii_row, 'buyValue'))} / "
                    f"sell {fmt_cr(_f(dii_row, 'sellValue'))})\n\n"
                    f"{DISCLAIMER}")
                dg["wrap"] = today.isoformat()
                dg["fiidii"] = today.isoformat()
                save_fn(state)
                print(f"[digest] market wrap + FII/DII posted for {today}")
        except Exception as exc:
            print(f"[digest] market wrap failed ({exc})")

    if (_in_window(now, 22, 30, 23, 0)
            and dg.get("recap") != today.isoformat()):
        try:
            entries = [e for e in state.get("archive", [])
                       if e.get("d") == today.isoformat()]
            if not entries:
                print(f"[digest] recap empty for {today}; "
                      "will retry in window")
            else:
                by_rating = {}
                for e in entries:
                    r = e.get("r") or "Results Filed"
                    key = "Filed" if r == "Results Filed" else r
                    during, after = by_rating.setdefault(key, ([], []))
                    (during if _during_market(e.get("t") or "")
                     else after).append(e.get("s"))
                by_rating = {k: (sorted(set(v[0])),
                                 sorted(set(v[1])))
                             for k, v in by_rating.items()}
                _post_image(
                    token, chat_id,
                    lambda p: render_recap_image(
                        fmt_date(today), by_rating, p),
                    _recap_caption(today, by_rating))
                dg["recap"] = today.isoformat()
                save_fn(state)
                print(f"[digest] recap posted for {today} "
                      f"({len(entries)} entries)")
        except Exception as exc:
            print(f"[digest] recap failed ({exc})")

    iso_year, iso_week, _ = today.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    if (now.weekday() == 6 and _in_window(now, 10, 0, 10, 30)
            and dg.get("actions") != week_key):
        try:
            events = fetch_nse_json(EVENTS_URL)
            by_day = _actions_by_day(today, events)
            if by_day:
                total = sum(len(s) for _, s in by_day)
                _post_image(
                    token, chat_id,
                    lambda p: render_actions_image(
                        f"Week from {fmt_date(today)}", by_day, p),
                    f"💰 Corporate Actions — meetings this week "
                    f"({total} companies)\n"
                    "Board meetings to consider dividend / bonus / "
                    "buyback. These are meeting dates, not ex-dates.\n"
                    "Details on the image.\n\n"
                    f"{DISCLAIMER}")
                print(f"[digest] actions image posted for {week_key}")
            else:
                print(f"[digest] actions digest empty for {week_key}")
            dg["actions"] = week_key
            save_fn(state)
        except Exception as exc:
            print(f"[digest] actions digest failed ({exc})")

    hour_key = now.strftime("%Y-%m-%d %H")
    if dg.get("ipo_check") != hour_key:
        try:
            ipos = fetch_nse_json(IPO_URL)
            gmp_rows = fetch_gmp()
            posted = set(dg.get("ipo_posted") or [])
            for ipo in ipos:
                if (ipo.get("status") or "") != "Active":
                    continue
                sym = ipo.get("symbol")
                if not sym or sym in posted:
                    continue
                company = ipo.get("companyName") or sym
                g = _match_gmp(company, gmp_rows)
                price = ipo.get("issuePrice")
                split = fetch_chittorgarh_split(company)
                cap = _upper_price(price)
                if split and split.get("price_low") \
                        and split.get("price_high"):
                    price = (f"₹{split['price_low']:,.0f} to "
                             f"₹{split['price_high']:,.0f}")
                    cap = float(split["price_high"])
                elif not price and g and g.get("price_band") is not None:
                    price = f"up to ₹{float(g['price_band']):,.0f}"
                    cap = float(g["price_band"])
                open_lbl = fmt_date(parse_nse_date(
                    ipo.get("issueStartDate"))) \
                    if parse_nse_date(ipo.get("issueStartDate")) else None
                close_lbl = fmt_date(parse_nse_date(
                    ipo.get("issueEndDate"))) \
                    if parse_nse_date(ipo.get("issueEndDate")) else None
                listing_lbl = None
                lot_lbl = None
                gmp_lbl = None
                est_lbl = None
                if split and split.get("lot"):
                    lot_lbl = f"{split['lot']} (chittorgarh.com)"
                if g:
                    if g.get("listing_date"):
                        listing_lbl = (_fmt_iso_date(g["listing_date"])
                                       or g["listing_date"]) \
                            + " (gmptoday.in)"
                    if lot_lbl is None and g.get("lot_size") is not None:
                        lot_lbl = (f"{float(g['lot_size']):,.0f} shares "
                                   "(gmptoday.in)")
                    if g.get("gmp_median") is not None:
                        gmp_lbl = (f"₹{float(g['gmp_median']):,.0f} "
                                   "(unofficial, gmptoday.in)")
                        if cap is not None:
                            est_lbl = (f"₹{cap + float(g['gmp_median']):,.0f} "
                                       f"(₹{cap:,.0f} + "
                                       f"₹{float(g['gmp_median']):,.0f})")
                issue_lbl = None
                fresh_lbl = None
                ofs_lbl = None
                if split:
                    issue_lbl = f"{split['total']} (chittorgarh.com)"
                    fresh_lbl = f"{split['fresh']} (chittorgarh.com)"
                    ofs_lbl = split["ofs"] if split["ofs"] else "None"
                elif g and g.get("issue_size"):
                    issue_lbl = f"{g['issue_size']} (gmptoday.in)"
                try:
                    sub = float(ipo.get("noOfTime"))
                    sub_lbl = f"{sub:.2f}x (NSE)"
                except (TypeError, ValueError):
                    sub_lbl = None
                _post_image(
                    token, chat_id,
                    lambda p, _c=company, _s=sym, _pr=price, _o=open_lbl,
                    _cl=close_lbl, _li=listing_lbl, _lo=lot_lbl,
                    _su=sub_lbl, _g=gmp_lbl, _e=est_lbl,
                    _i=issue_lbl, _f=fresh_lbl, _of=ofs_lbl: render_ipo_image(
                        _c, _s, _pr, _o, _cl, _li, _lo, _su, _g, _e, p,
                        issue_size=_i, fresh_issue=_f, ofs=_of),
                    f"{company} ({sym}) IPO is open — {price or 'price not disclosed'}"
                    f" — {ipo.get('issueStartDate')} to "
                    f"{ipo.get('issueEndDate')}"
                    f" — subscribed {sub_lbl or 'n/a'}."
                    + (f" Issue size {issue_lbl}." if issue_lbl else "")
                    + (f" Fresh {fresh_lbl}, OFS {ofs_lbl}."
                       if fresh_lbl else "")
                    + (f" GMP {gmp_lbl}." if gmp_lbl else "")
                    + f"\n\n{IPO_DISCLAIMER}")
                posted.add(sym)
                print(f"[digest] IPO card posted for {sym}")
            dg["ipo_posted"] = sorted(posted)
            dg["ipo_check"] = hour_key
            save_fn(state)
        except Exception as exc:
            print(f"[digest] IPO check failed ({exc})")
