# Share Market Charts — Earnings Poster

Fully automatic earnings poster: every 15 minutes it watches NSE's
corporate-announcements feed, downloads each new results filing PDF,
reads the numbers, renders an image card, and posts it to the
Telegram channel with "Result ↗" (filing) and "Chart ↗" (TradingView)
buttons. Free on a public GitHub repo.

## Setup, step by step

**Step 1 — New bot token (Telegram, 2 minutes).**
Open @BotFather → `/mybots` → select your bot → API Token →
**Revoke current token**. BotFather gives a NEW token. Keep it
private. Never paste it into code or chat; it goes into GitHub
Secrets in Step 4. (The old token appeared in chat earlier, so it
must be treated as exposed.)

**Step 2 — Bot as channel admin.**
In Share Market Charts: Edit → Administrators → add the bot, with
permission to post messages. Without this, nothing can post.

**Step 3 — Code.**
This repo is the program. Nothing in it contains any token or
password; all secrets come from GitHub Secrets at run time.

**Step 4 — Public repo + secrets (GitHub, 5 minutes).**
Create a PUBLIC repo (a 15-minute schedule exceeds the private
repo's free 2,000 minutes/month; public repos are unlimited),
upload these files, then add two Secrets (Settings → Secrets and
variables → Actions → New repository secret), typed by you:
- `TELEGRAM_BOT_TOKEN` — the NEW token from Step 1.
- `TELEGRAM_CHAT_ID` — the channel's ID (from @getmyid or the
  channel's numeric ID; it usually starts with `-100`).

**Step 5 — First run + live test.**
Run the workflow once manually (Actions → Earnings poster → Run
workflow). The first run only saves a baseline and posts nothing,
so there is no spam. Then, on the next real filing (or a test run
you approve), verify the card appears in Share Market Charts with
both buttons. This first live run also settles the one untested
question: whether NSE accepts GitHub's servers (it blocks Google's
servers, which is why the Sheet-only route could not do this).

**Step 6 — It runs itself.**
Every 15 minutes from then on. A monthly keepalive run keeps
GitHub's scheduler enabled (scheduled workflows are disabled after
60 days of repo inactivity).

## How it works, honestly

- Data source: NSE's own announcements feed + the company's official
  filing PDF. No Screener, no news site, no paid feed.
- Cadence is ~15-minute polling, not seconds-fast. Scheduling can
  drift by a few minutes.
- The first results table in a filing is used: standalone when the
  filing has one, otherwise consolidated.
- PAT Margin = PAT / Revenue, the same definition the filings' own
  ratio tables use. If a filing has no clean operating-profit row,
  the card shows Total Income and PAT Margin instead — an operating
  profit figure is never invented.
- Ratings are rule-based from filed numbers (Excellent / Good /
  Positive / OK / Weak / Bad) with a reason line, not an AI claim.
- If a PDF cannot be downloaded, it is NOT marked seen, so the next
  run retries it (delayed, never lost). If the NSE feed itself is
  unreachable (it blocks intermittently), the run ends quietly with
  no state change and retries 15 minutes later. If numbers cannot be parsed
  or fail the Total Income cross-check (Total = Revenue + Other
  Income), the watcher posts "Results Filed" + link only, never
  guessed numbers. Filings older than 7 days are skipped.
- Known limit: PDF text extraction occasionally mangles brackets
  around loss figures. The cross-check catches most corruption, but
  loss brackets are the weakest point of any PDF-text parser.
- The repo is public, so the code is visible to anyone — but the
  token lives only in GitHub Secrets, never in code.
