#!/usr/bin/env python3
"""Real-time Tokyo stock price alerts pushed to your phone via ntfy.

Price source: Yahoo! Finance Japan, which shows TSE prices in real time
(labelled リアルタイム株価 on the page). If that page can't be read, the script
falls back to Yahoo Finance (US) and clearly marks the alert as DELAYED.

Timing: instead of relying on GitHub's cron (often late by 5-15+ min), the job
starts shortly before a trading session and sends an alert exactly on every
5-minute mark itself (09:00, 09:05, ... 11:30 and 12:30 ... 15:30 JST).

Usage:
  python3 price_alert.py         alert through the current/next session, then exit
  python3 price_alert.py --now   send one alert immediately first (used for manual test runs)

Environment variables:
  NTFY_TOPIC    required  your private ntfy topic (treat it like a password)
  CODE          optional  TSE stock code, default 6098 (Recruit Holdings)
  INTERVAL_MIN  optional  minutes between alerts, default 5
  NTFY_SERVER   optional  default https://ntfy.sh
  ALERT_ABOVE   optional  price level; at or above it the alert is sent as urgent
  ALERT_BELOW   optional  price level; at or below it the alert is sent as urgent
  DRY_RUN       optional  1 = print alerts instead of sending them

Standard library only, nothing to install.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
CODE = os.environ.get("CODE", "6098")
INTERVAL = int(os.environ.get("INTERVAL_MIN", "5"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
SESSIONS = [((9, 0), (11, 30)), ((12, 30), (15, 30))]  # TSE, JST
LEAD = timedelta(minutes=45)  # a run only waits for a session starting within this window
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}

SIGNED = r"[+\-−－]?\d[\d,]*(?:\.\d+)?"
BOARD_RE = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"     # current price
    r"[^0-9]{0,40}?前日比[^0-9+\-−－]{0,20}"
    r"(" + SIGNED + r")"                              # change
    r"\s*[（(]\s*(" + SIGNED + r")\s*%\s*[)）]"        # change %
)
STAMP_RE = re.compile(r"[^0-9]{0,20}?(\d{1,2}[:/]\d{2})")


def now_jst():
    return datetime.now(JST)


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def to_num(s):
    s = s.replace(",", "").replace("−", "-").replace("－", "-").replace("+", "")
    return float(s)


def page_text(page):
    page = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"\s+", " ", text)


def parse_yahoo_jp(page):
    """Find the main price board: '<price> 前日比 <chg>(<pct>%) リアルタイム株価 <HH:MM>'."""
    text = page_text(page)
    for label in re.finditer("リアルタイム株価", text):
        window = text[max(0, label.start() - 200):label.start()]
        found = list(BOARD_RE.finditer(window))
        if not found:
            continue
        m = found[-1]
        stamp = STAMP_RE.match(text[label.end():label.end() + 40])
        price, chg = to_num(m.group(1)), to_num(m.group(2))
        return {
            "price": price,
            "chg": chg,
            "pct": to_num(m.group(3)),
            "prev": price - chg,
            "stamp": stamp.group(1) if stamp else None,
            "realtime": True,
            "source": "Yahoo!ファイナンス",
        }
    raise ValueError("real-time price board not found on Yahoo! Finance Japan page")


def from_yahoo_jp():
    return parse_yahoo_jp(http_get(f"https://finance.yahoo.co.jp/quote/{CODE}.T"))


def from_yahoo_us():
    """Fallback only: ~20 min delayed."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(f"{CODE}.T")
        + "?interval=1m&range=1d"
    )
    meta = json.loads(http_get(url))["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    ts = meta.get("regularMarketTime")
    chg = price - float(prev) if prev else None
    return {
        "price": price,
        "chg": chg,
        "pct": chg / float(prev) * 100 if prev else None,
        "prev": float(prev) if prev else None,
        "stamp": datetime.fromtimestamp(ts, JST).strftime("%H:%M") if ts else None,
        "realtime": False,
        "source": "Yahoo Finance US",
    }


def fmt(x):
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.1f}"


def sign(x):
    return "+" if x >= 0 else "-"


def build(q):
    title = f"{CODE} ¥{fmt(q['price'])}"
    lines, tags, priority = [], ["bar_chart"], 3
    if q["chg"] is not None:
        title += f"  {sign(q['chg'])}{abs(q['pct']):.2f}%"
        lines.append(f"{sign(q['chg'])}¥{fmt(abs(q['chg']))} vs prev close ¥{fmt(q['prev'])}")
        tags = ["chart_with_upwards_trend" if q["chg"] >= 0 else "chart_with_downwards_trend"]
    if not q["realtime"]:
        title = "[DELAYED] " + title
        tags = ["warning"] + tags
        lines.append("Yahoo! Finance Japan could not be read, so this quote is ~20 min delayed.")

    above, below = os.environ.get("ALERT_ABOVE"), os.environ.get("ALERT_BELOW")
    if above and q["price"] >= float(above):
        priority, tags = 5, ["rotating_light"] + tags
        lines.insert(0, f"At or above your ¥{fmt(float(above))} level")
    if below and q["price"] <= float(below):
        priority, tags = 5, ["rotating_light"] + tags
        lines.insert(0, f"At or below your ¥{fmt(float(below))} level")

    stamp = q["stamp"] or "--:--"
    kind = "real-time" if q["realtime"] else "delayed"
    lines.append(f"Quote {stamp} JST ({kind}) · {q['source']} · sent {now_jst():%H:%M:%S}")
    return title, "\n".join(lines), tags, priority


def notify(title, message, tags, priority=3):
    payload = {
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": message,
        "tags": tags,
        "priority": priority,
        "click": f"https://finance.yahoo.co.jp/quote/{CODE}.T",
    }
    if os.environ.get("DRY_RUN") == "1":
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def alert_once():
    quote, errors = None, []
    for fetch in (from_yahoo_jp, from_yahoo_us):
        try:
            quote = fetch()
            break
        except Exception as exc:
            errors.append(f"{fetch.__name__}: {exc}")
    for e in errors:
        print("warn:", e, file=sys.stderr, flush=True)
    if quote is None:
        notify(f"{CODE}: price unavailable", "Both price sources failed this check.", ["warning"], 2)
        return
    title, message, tags, priority = build(quote)
    print(f"{now_jst():%H:%M:%S}", title, "|", message.replace("\n", " | "), flush=True)
    notify(title, message, tags, priority)


def session_marks(now):
    """5-minute marks for the session in progress, or one starting within LEAD; else []."""
    for (sh, sm), (eh, em) in SESSIONS:
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if now <= end and start - now <= LEAD:
            marks, t = [], start
            while t <= end:
                if t >= now - timedelta(seconds=20):
                    marks.append(t)
                t += timedelta(minutes=INTERVAL)
            return marks
    return []


def main():
    if "--now" in sys.argv:
        alert_once()
    if now_jst().weekday() >= 5:
        print("Weekend in Tokyo, nothing to do.", flush=True)
        return
    marks = session_marks(now_jst())
    if not marks:
        print("No trading session now or within the next 45 min.", flush=True)
        return
    print(f"Alerting {len(marks)} times, {marks[0]:%H:%M}-{marks[-1]:%H:%M} JST", flush=True)
    for mark in marks:
        wait = (mark - now_jst()).total_seconds()
        if wait > 0:
            time.sleep(wait)
        try:
            alert_once()
        except Exception as exc:  # never let one bad check stop the session
            print("error:", exc, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
