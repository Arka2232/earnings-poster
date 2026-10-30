"""Scheduled digest posts for Share Market Charts (morning calendar,
daily market wrap, weekly corporate-actions digest, IPO section).

Runs inside the same 5-minute poster workflow: watcher.py calls
maybe_run_digests() after the results filings, guarded so a digest
failure can never break results posting.

Sources (all NSE public JSON, free, no key; same header pattern as
the poster — browser User-Agent + Referer, with retries because NSE
403s intermittently):
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
- GMP is never posted (unofficial and unregulated).
- Every post ends with the education-only disclaimer.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import requests

from telegram_post import send_text

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

DISCLAIMER = ("For education only — not investment advice. "
              "I am not SEBI-registered.")

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


def build_morning_post(today, events, ipos):
    """Today's results + next few days + open IPOs."""
    today_rows = sorted(
        (e for e in events
         if parse_nse_date(e.get("date")) == today
         and _is_results_purpose(e.get("purpose"))),
        key=lambda e: e.get("symbol") or "")
    lines = [f"📅 Results Calendar — {fmt_date(today)}", ""]
    if today_rows:
        lines.append(f"Today's results ({len(today_rows)}):")
        lines.append(", ".join(e.get("symbol") for e in today_rows))
    else:
        lines.append("No results scheduled for today.")
    lines.append("")
    upcoming = []
    for offset in (1, 2, 3):
        day = today + timedelta(days=offset)
        n = sum(1 for e in events
                if parse_nse_date(e.get("date")) == day
                and _is_results_purpose(e.get("purpose")))
        if n:
            upcoming.append(f"{day.strftime('%d %b')} — {n} companies")
    if upcoming:
        lines.append("Coming up:")
        lines.extend(upcoming)
        lines.append("")
    active = [i for i in ipos if (i.get("status") or "") == "Active"]
    if active:
        lines.append("📈 Open IPOs:")
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
            lines.append(
                f"{ipo.get('companyName')} ({ipo.get('symbol')}) — "
                f"{ipo.get('issueStartDate')} to {ipo.get('issueEndDate')} — "
                f"{price} — {sub_txt}{closing}".strip())
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


def build_actions_post(today, events):
    """Upcoming board meetings to consider dividend/bonus/buyback.

    Returns None when there is nothing in the next 7 days (the week
    is then skipped silently). These are MEETING dates, not ex-dates.
    """
    horizon = [today + timedelta(days=i) for i in range(7)]
    by_day = {}
    for day in horizon:
        syms = sorted(e.get("symbol") for e in events
                      if parse_nse_date(e.get("date")) == day
                      and _is_action_purpose(e.get("purpose")))
        if syms:
            by_day[day] = syms
    if not by_day:
        return None
    lines = ["💰 Corporate Actions — meetings this week", "",
             "Board meetings to consider dividend / bonus / buyback. "
             "These are meeting dates, not ex-dates.", ""]
    for day in horizon:
        if day in by_day:
            lines.append(f"{day.strftime('%d %b')}: "
                         + ", ".join(by_day[day]))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _in_window(now, start_h, start_m, end_h, end_m):
    t = now.hour * 60 + now.minute
    return start_h * 60 + start_m <= t < end_h * 60 + end_m


def maybe_run_digests(token, chat_id, state, save_fn, now=None):
    """Run whichever digest gates are open. Flags in state["digest"]
    make each digest fire once per period; a failed fetch leaves the
    flag unset so the next 5-minute run retries inside the window."""
    now = now or ist_now()
    today = now.date()
    dg = state.setdefault("digest", {})

    if (_in_window(now, 8, 0, 8, 30)
            and dg.get("calendar") != today.isoformat()):
        try:
            events = fetch_nse_json(EVENTS_URL)
            ipos = fetch_nse_json(IPO_URL)
            send_long_text(token, chat_id,
                           build_morning_post(today, events, ipos))
            dg["calendar"] = today.isoformat()
            save_fn(state)
            print(f"[digest] morning calendar posted for {today}")
        except Exception as exc:
            print(f"[digest] morning calendar failed ({exc})")

    if (now.weekday() < 5 and _in_window(now, 19, 30, 20, 0)
            and dg.get("wrap") != today.isoformat()):
        try:
            indices = fetch_nse_json(INDICES_URL)
            fiidii = fetch_nse_json(FIIDII_URL)
            send_long_text(token, chat_id,
                           build_wrap_post(today, indices, fiidii))
            dg["wrap"] = today.isoformat()
            save_fn(state)
            print(f"[digest] market wrap posted for {today}")
        except Exception as exc:
            print(f"[digest] market wrap failed ({exc})")

    iso_year, iso_week, _ = today.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    if (now.weekday() == 6 and _in_window(now, 10, 0, 10, 30)
            and dg.get("actions") != week_key):
        try:
            events = fetch_nse_json(EVENTS_URL)
            post = build_actions_post(today, events)
            if post:
                send_long_text(token, chat_id, post)
                print(f"[digest] actions digest posted for {week_key}")
            else:
                print(f"[digest] actions digest empty for {week_key}")
            dg["actions"] = week_key
            save_fn(state)
        except Exception as exc:
            print(f"[digest] actions digest failed ({exc})")
