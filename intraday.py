"""
intraday.py — runs every 15 min during Indian market hours.
Detects volume bursts in the most recent 15-min window vs the same
time-of-day across the past 5 trading sessions.

Why this exists: the daily volume detector only fires at EOD because
intraday volume is partial-day. This catches surges in real time.
"""
import os, json, logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import yfinance as yf
import numpy as np
import httpx
import psycopg2
import psycopg2.extras
from groq import Groq

PG_DSN   = os.environ["SUPABASE_PG_DSN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT  = os.environ["TG_CHAT_ID"]

WATCHLIST = {
    "SBIN.NS":     ["state bank of india", "sbi "],
    "RELIANCE.NS": ["reliance industries", "ril ", "mukesh ambani", "jio"],
}

INTRADAY_Z_THRESHOLD = 3.0   # higher than daily — intraday is noisier
LOOKBACK_SESSIONS    = 5
HORIZON_HOURS        = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("intraday")

@contextmanager
def db_cur():
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    finally:
        conn.close()

def in_market_hours():
    """Indian market: Mon-Fri, 09:15 to 15:30 IST = 03:45 to 10:00 UTC."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:           # Sat/Sun
        return False
    minutes = now.hour * 60 + now.minute
    return 3*60 + 45 <= minutes <= 10*60

def detect():
    if not in_market_hours():
        log.info("outside market hours — skipping")
        return []

    hits = []
    for sym in WATCHLIST:
        try:
            # 8 days of 15-min bars covers ~5 sessions safely
            hist = yf.Ticker(sym).history(period="8d", interval="15m")
            if len(hist) < 50:
                continue

            # Latest bar = current 15-min window (still building)
            latest = hist.iloc[-1]
            latest_ts = hist.index[-1]
            latest_v = float(latest["Volume"])
            if latest_v == 0:
                continue

            # Build baseline: same time-of-day across past sessions, excluding today
            tod = (latest_ts.hour, latest_ts.minute)
            today_date = latest_ts.date()
            same_tod_vols = [
                float(hist.iloc[i]["Volume"])
                for i in range(len(hist) - 1)
                if (hist.index[i].hour, hist.index[i].minute) == tod
                and hist.index[i].date() != today_date
                and hist.iloc[i]["Volume"] > 0
            ][-LOOKBACK_SESSIONS:]

            if len(same_tod_vols) < 3:
                continue

            mu, sigma = float(np.mean(same_tod_vols)), float(np.std(same_tod_vols, ddof=1))
            if sigma == 0:
                continue
            z = (latest_v - mu) / sigma
            if z < INTRADAY_Z_THRESHOLD:
                continue

            # Direction: sign of last bar's return
            close = float(latest["Close"])
            prev_close = float(hist.iloc[-2]["Close"])
            ret_pct = (close / prev_close - 1) * 100
            direction = "up" if ret_pct >= 0 else "down"

            hits.append({
                "symbol": sym,
                "ts": latest_ts.to_pydatetime().isoformat(),
                "score": round(z, 2),
                "direction": direction,
                "entry_price": close,
                "payload": {
                    "z": round(z, 2),
                    "window": f"{tod[0]:02d}:{tod[1]:02d} (15-min)",
                    "current_volume": int(latest_v),
                    "baseline_mean": int(mu),
                    "baseline_n": len(same_tod_vols),
                    "bar_return_pct": round(ret_pct, 2),
                    "close": close,
                }
            })
        except Exception as ex:
            log.warning(f"{sym}: {ex}")
    log.info(f"intraday bursts detected: {len(hits)}")
    return hits

def recent_events(symbol, hours=6):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with db_cur() as c:
        c.execute(
            "SELECT title, url FROM events WHERE %s = ANY(tickers) AND ts >= %s "
            "ORDER BY ts DESC LIMIT 3",
            (symbol, cutoff)
        )
        return c.fetchall()

def thesis(symbol, a, events):
    catalyst = ("Recent matched news:\n" + "\n".join(f"- {e['title']}" for e in events)
                if events else "No matched news in last 6h — possible pre-news positioning.")
    prompt = f"""Cautious quant. Intraday volume burst detected.

Symbol: {symbol}
Window: {a['payload']['window']}
Volume z-score (vs same time last {a['payload']['baseline_n']} sessions): {a['payload']['z']}
This bar's return: {a['payload']['bar_return_pct']}%
Price: ₹{a['payload']['close']}

{catalyst}

Output 4 lines:
THESIS: <one sentence>
EVIDENCE: <one sentence>
CONFIDENCE: low|medium|high — <why>
INVALIDATION: <specific condition>"""
    try:
        r = Groq(api_key=GROQ_KEY).chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=300,
        ).choices[0].message.content.strip()
        conf = "low"
        for line in r.splitlines():
            if line.upper().startswith("CONFIDENCE:"):
                low = line.lower()
                if "high" in low: conf = "high"
                elif "medium" in low: conf = "medium"
        return r, conf
    except Exception as ex:
        return f"(LLM error: {ex})", "low"

def log_and_alert(a, text, confidence, events):
    with db_cur() as c:
        c.execute(
            "INSERT INTO signals (ts, symbol, detector, score, direction, entry_price, "
            "horizon_hours, payload, thesis, confidence) "
            "VALUES (%s,%s,'intraday_volume_burst',%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ts, symbol, detector) DO NOTHING RETURNING id",
            (a["ts"], a["symbol"], a["score"], a["direction"], a["entry_price"],
             HORIZON_HOURS, json.dumps(a["payload"]), text, confidence)
        )
        row = c.fetchone()
        if not row:
            return

    link_line = f"\n📰 [{events[0]['title']}]({events[0]['url']})" if events else "\n📰 _no recent news_"
    msg = (f"⚡ *{a['symbol']}* INTRADAY BURST\n"
           f"z={a['score']:.1f} in {a['payload']['window']} window | "
           f"bar ret={a['payload']['bar_return_pct']}% | ₹{a['payload']['close']:.2f}\n"
           f"_Direction: {a['direction']} | Horizon: {HORIZON_HOURS}h_\n\n"
           f"```\n{text}\n```{link_line}\n\n"
           f"📊 Tracked as paper-trade signal #{row['id']}.")
    httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
               json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    log.info(f"alerted intraday burst #{row['id']} {a['symbol']}")

def main():
    for a in detect():
        ev = recent_events(a["symbol"])
        text, conf = thesis(a["symbol"], a, ev)
        log_and_alert(a, text, conf, ev)

if __name__ == "__main__":
    main()
