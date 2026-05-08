"""
poll.py — runs every 30 min via GitHub Actions
  1. pull RSS news, tag tickers, store in `events`
  2. pull NSE bulk deals (when available), store in `bulk_deals`
  3. fetch yfinance daily bars, run volume z-score detector
  4. for each NEW signal: log to `signals` with entry price + send Telegram
"""
import os, hashlib, json, logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import feedparser
import yfinance as yf
import numpy as np
import httpx
import psycopg2
import psycopg2.extras
from groq import Groq

# ─────────── config ───────────
PG_DSN   = os.environ["SUPABASE_PG_DSN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT  = os.environ["TG_CHAT_ID"]

WATCHLIST = ["SBIN.NS", "RELIANCE.NS", "INFY.NS", "TCS.NS", "LT.NS", "SBIN.NS"]

FEEDS = [
    "https://www.livemint.com/rss/markets",
    "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://news.google.com/rss/search?q=NSE+India&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=SEBI+disclosure&hl=en-IN&gl=IN&ceid=IN:en",
    "https://www.rbi.org.in/Scripts/Rss.aspx?Cat=53",  # RBI press releases
]

VOL_Z_THRESHOLD = 2.5
LOOKBACK_DAYS   = 20
DEFAULT_HORIZON = 24

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macrolens")

# ─────────── db ───────────
@contextmanager
def db_cur():
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db_cur() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id        TEXT PRIMARY KEY,
            ts        TIMESTAMPTZ,
            source    TEXT, title TEXT, body TEXT, url TEXT,
            tickers   TEXT[]
        );
        CREATE TABLE IF NOT EXISTS bulk_deals (
            id           TEXT PRIMARY KEY,
            deal_date    DATE, symbol TEXT, client_name TEXT,
            buy_sell     TEXT, qty BIGINT, avg_price NUMERIC,
            inserted_at  TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS signals (
            id            BIGSERIAL PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL,
            symbol        TEXT NOT NULL,
            detector      TEXT NOT NULL,
            score         REAL NOT NULL,
            direction     TEXT,
            entry_price   NUMERIC,
            horizon_hours INT DEFAULT 24,
            payload       JSONB,
            thesis        TEXT,
            confidence    TEXT,
            alerted_at    TIMESTAMPTZ DEFAULT now(),
            UNIQUE(ts, symbol, detector)
        );
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            signal_id     BIGINT PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
            scored_at_1d  TIMESTAMPTZ, exit_price_1d NUMERIC,
            return_1d_pct REAL, hit_1d BOOLEAN,
            scored_at_5d  TIMESTAMPTZ, exit_price_5d NUMERIC,
            return_5d_pct REAL, hit_5d BOOLEAN
        );
        """)

# ─────────── news ───────────
def _eid(*parts): return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

def _match_tickers(text):
    t = " " + text.lower() + " "
    return [s for s, kws in WATCHLIST.items() if any(k in t for k in kws)]

def poll_news():
    saved = 0
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:50]:
                title = e.get("title", "")
                summary = e.get("summary", "")
                link = e.get("link", "")
                ts_raw = e.get("published_parsed")
                ts = datetime(*ts_raw[:6], tzinfo=timezone.utc).isoformat() if ts_raw \
                     else datetime.now(timezone.utc).isoformat()
                tickers = _match_tickers(f"{title} {summary}")
                if not tickers:
                    continue
                with db_cur() as c:
                    c.execute(
                        "INSERT INTO events VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (_eid(url, link, title), ts, url, title, summary, link, tickers)
                    )
                    if c.rowcount:
                        saved += 1
        except Exception as ex:
            log.warning(f"feed {url} failed: {ex}")
    log.info(f"news: +{saved} tagged events")

# ─────────── NSE bulk deals (best-effort; NSE blocks bots sometimes) ───────────
def poll_bulk_deals():
    today = datetime.now(timezone.utc).strftime("%d-%m-%Y")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as cli:
            cli.get("https://www.nseindia.com")  # session cookie
            r = cli.get(f"https://www.nseindia.com/api/historical/bulk-deals?from={today}&to={today}")
            if r.status_code != 200:
                log.warning(f"NSE bulk deals: HTTP {r.status_code}")
                return
            rows = r.json().get("data", [])
            with db_cur() as c:
                for d in rows:
                    key = _eid(d.get("date",""), d.get("symbol",""), d.get("clientName",""), d.get("buySell",""), str(d.get("quantity","")))
                    c.execute(
                        "INSERT INTO bulk_deals VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
                        "ON CONFLICT (id) DO NOTHING",
                        (key, d.get("date"), d.get("symbol"), d.get("clientName"),
                         d.get("buySell"), int(d.get("quantity") or 0),
                         float(d.get("avgPrice") or 0))
                    )
            log.info(f"bulk deals: pulled {len(rows)}")
    except Exception as ex:
        log.warning(f"bulk deals failed: {ex}")

# ─────────── volume z-score detector ───────────
def detect_volume_anomalies():
    hits = []
    for sym in WATCHLIST:
        try:
            hist = yf.Ticker(sym).history(period=f"{LOOKBACK_DAYS+5}d", interval="1d")
            if len(hist) < LOOKBACK_DAYS:
                continue
            vols     = hist["Volume"].values[:-1]
            today_v  = float(hist["Volume"].iloc[-1])
            mu, sigma = float(vols.mean()), float(vols.std(ddof=1))
            if sigma == 0: continue
            z = (today_v - mu) / sigma
            if z < VOL_Z_THRESHOLD: continue
            close      = float(hist["Close"].iloc[-1])
            ret_pct    = (close / float(hist["Close"].iloc[-2]) - 1) * 100
            direction  = "up" if ret_pct >= 0 else "down"  # naive: ride the move
            ts         = hist.index[-1].to_pydatetime().isoformat()
            hits.append({
                "symbol": sym, "ts": ts, "score": round(z, 2),
                "direction": direction, "entry_price": close,
                "payload": {
                    "z": round(z, 2),
                    "today_volume": int(today_v),
                    "baseline_mean_volume": int(mu),
                    "return_pct": round(ret_pct, 2),
                    "close": close,
                }
            })
        except Exception as ex:
            log.warning(f"{sym} fetch failed: {ex}")
    log.info(f"volume anomalies: {len(hits)}")
    return hits

# ─────────── correlation ───────────
def recent_events_for(symbol, hours=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with db_cur() as c:
        c.execute(
            "SELECT title, url FROM events WHERE %s = ANY(tickers) AND ts >= %s "
            "ORDER BY ts DESC LIMIT 5",
            (symbol, cutoff)
        )
        return c.fetchall()

# ─────────── LLM thesis ───────────
def generate_thesis(symbol, anomaly, events):
    catalyst = ("Recent matched news:\n" + "\n".join(f"- {e['title']}" for e in events[:3])
                if events else
                "NO matched news in last 24h. Possible: pre-news positioning, sector driver, or noise — be skeptical.")
    prompt = f"""You are a cautious senior quant analyst writing a personal alert. Never claim certainty.

Symbol: {symbol}
Volume z-score: {anomaly['payload']['z']} vs 20-day baseline
Today's return: {anomaly['payload']['return_pct']}%
Today's close: ₹{anomaly['payload']['close']}

{catalyst}

Output EXACTLY 4 lines, no preamble:
THESIS: <one concrete sentence>
EVIDENCE: <one sentence citing volume z, return, news (or absence)>
CONFIDENCE: low|medium|high — <one short reason>
INVALIDATION: <specific price/volume condition that kills the thesis>"""
    try:
        client = Groq(api_key=GROQ_KEY)
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=300,
        )
        text = resp.choices[0].message.content.strip()
        conf = "low"
        for line in text.splitlines():
            if line.upper().startswith("CONFIDENCE:"):
                low = line.lower()
                if "high" in low: conf = "high"
                elif "medium" in low: conf = "medium"
        return text, conf
    except Exception as ex:
        log.warning(f"LLM failed: {ex}")
        return "(LLM unavailable — check raw stats above.)", "low"

# ─────────── alerts + log signal ───────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    httpx.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown",
                          "disable_web_page_preview": False}, timeout=15)

def log_and_alert(anomaly, thesis, confidence, events):
    with db_cur() as c:
        c.execute(
            "INSERT INTO signals (ts, symbol, detector, score, direction, entry_price, "
            "horizon_hours, payload, thesis, confidence) "
            "VALUES (%s,%s,'volume_zscore',%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ts, symbol, detector) DO NOTHING RETURNING id",
            (anomaly["ts"], anomaly["symbol"], anomaly["score"],
             anomaly["direction"], anomaly["entry_price"], DEFAULT_HORIZON,
             json.dumps(anomaly["payload"]), thesis, confidence)
        )
        row = c.fetchone()
        if not row:
            return  # already logged
    link_line = f"\n📰 [{events[0]['title']}]({events[0]['url']})" if events else "\n📰 _no matched news (interesting)_"
    msg = (f"🔔 *{anomaly['symbol']}* | z={anomaly['score']:.1f} "
           f"| ret={anomaly['payload']['return_pct']}% | ₹{anomaly['payload']['close']:.2f}\n"
           f"_Direction guess: {anomaly['direction']} | Horizon: {DEFAULT_HORIZON}h_\n\n"
           f"```\n{thesis}\n```{link_line}\n\n"
           f"📊 Tracked as paper-trade signal #{row['id']}.")
    send_telegram(msg)
    log.info(f"alerted+logged signal #{row['id']} for {anomaly['symbol']}")

# ─────────── main ───────────
def main():
    init_db()
    poll_news()
    poll_bulk_deals()
    for a in detect_volume_anomalies():
        events = recent_events_for(a["symbol"])
        thesis, conf = generate_thesis(a["symbol"], a, events)
        log_and_alert(a, thesis, conf, events)

if __name__ == "__main__":
    main()
