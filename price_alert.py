#!/usr/bin/env python3
"""Push the latest Tokyo price of a stock (default 6098.T, Recruit Holdings) to your phone via ntfy.

Environment variables:
  NTFY_TOPIC    required  your private ntfy topic name (treat it like a password)
  TICKER        optional  Yahoo ticker, default 6098.T
  NTFY_SERVER   optional  default https://ntfy.sh
  ALERT_ABOVE   optional  price level; at or above it the notification is sent as urgent
  ALERT_BELOW   optional  price level; at or below it the notification is sent as urgent
  DRY_RUN       optional  set to 1 to print the notification instead of sending it

Uses only the Python standard library, so nothing needs installing.
"""
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
TICKER = os.environ.get("TICKER", "6098.T")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")


def from_yahoo(ticker):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1m&range=1d"
    )
    meta = json.loads(http_get(url))["chart"]["result"][0]["meta"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    ts = meta.get("regularMarketTime")
    return {
        "price": float(meta["regularMarketPrice"]),
        "prev": float(prev) if prev else None,
        "time": datetime.fromtimestamp(ts, JST) if ts else None,
        "source": "Yahoo",
    }


def from_stooq(ticker):
    """Fallback source. Gives the last price only (no previous close)."""
    code = ticker.split(".")[0].lower() + ".jp"
    url = f"https://stooq.com/q/l/?s={code}&f=sd2t2ohlcv&h&e=csv"
    row = next(csv.DictReader(io.StringIO(http_get(url))))
    return {
        "price": float(row["Close"]),  # ValueError if Stooq returns N/D
        "prev": None,
        "time": None,
        "source": "Stooq",
    }


def num(x):
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.1f}"


def build(quote):
    price = quote["price"]
    title = f"{TICKER} ¥{num(price)}"
    tags = ["bar_chart"]
    priority = 3
    lines = []

    if quote["prev"]:
        chg = price - quote["prev"]
        pct = chg / quote["prev"] * 100
        sign = "+" if chg >= 0 else "-"
        title += f"  {sign}{abs(pct):.2f}%"
        lines.append(f"{sign}¥{num(abs(chg))} vs prev close ¥{num(quote['prev'])}")
        tags = ["chart_with_upwards_trend" if chg >= 0 else "chart_with_downwards_trend"]

    above = os.environ.get("ALERT_ABOVE")
    below = os.environ.get("ALERT_BELOW")
    if above and price >= float(above):
        priority, tags = 5, ["rotating_light"] + tags
        lines.insert(0, f"At or above your ¥{num(float(above))} level")
    if below and price <= float(below):
        priority, tags = 5, ["rotating_light"] + tags
        lines.insert(0, f"At or below your ¥{num(float(below))} level")

    if quote["time"]:
        stamp = "quote " + quote["time"].strftime("%H:%M JST")
    else:
        stamp = "checked " + datetime.now(JST).strftime("%H:%M JST")
    lines.append(f"{stamp} · {quote['source']}")
    return title, "\n".join(lines), tags, priority


def notify(title, message, tags, priority=3):
    payload = {
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": message,
        "tags": tags,
        "priority": priority,
    }
    if os.environ.get("DRY_RUN") == "1":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main():
    quote, errors = None, []
    for fetch in (from_yahoo, from_stooq):
        try:
            quote = fetch(TICKER)
            break
        except Exception as exc:  # try the next source
            errors.append(f"{fetch.__name__}: {exc}")

    if quote is None:
        print("\n".join(errors), file=sys.stderr)
        notify(f"{TICKER}: price unavailable", "Both price sources failed this check.", ["warning"], 2)
        return

    title, message, tags, priority = build(quote)
    print(title, "|", message.replace("\n", " | "))
    notify(title, message, tags, priority)


if __name__ == "__main__":
    main()
