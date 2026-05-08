# score.py — runs daily at 11:00 UTC (4:30 PM IST, after market close)
import os, logging
from datetime import datetime, timedelta, timezone
import yfinance as yf, psycopg2, psycopg2.extras
from contextlib import contextmanager

PG_DSN = os.environ["SUPABASE_PG_DSN"]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("score")

@contextmanager
def cur():
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cu:
            yield cu
        conn.commit()
    finally:
        conn.close()

def latest_close(sym):
    h = yf.Ticker(sym).history(period="3d", interval="1d")
    return float(h["Close"].iloc[-1]) if len(h) else None

def score_horizon(days, col_at, col_px, col_ret, col_hit):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with cur() as c:
        c.execute(f"""
          SELECT s.id, s.symbol, s.entry_price, s.direction
          FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id = s.id
          WHERE s.ts <= %s AND (o.{col_at} IS NULL OR o.signal_id IS NULL)
        """, (cutoff,))
        rows = c.fetchall()

    for r in rows:
        px = latest_close(r["symbol"])
        if not px or not r["entry_price"]: continue
        ret = (px / float(r["entry_price"]) - 1) * 100
        hit = (ret > 0) if r["direction"] == "up" else (ret < 0) if r["direction"] == "down" else None
        with cur() as c:
            c.execute(f"""
              INSERT INTO signal_outcomes (signal_id, {col_at}, {col_px}, {col_ret}, {col_hit})
              VALUES (%s, now(), %s, %s, %s)
              ON CONFLICT (signal_id) DO UPDATE SET
                {col_at}=EXCLUDED.{col_at}, {col_px}=EXCLUDED.{col_px},
                {col_ret}=EXCLUDED.{col_ret}, {col_hit}=EXCLUDED.{col_hit}
            """, (r["id"], px, round(ret, 2), hit))
        log.info(f"scored signal #{r['id']} ({r['symbol']}) {days}d: {ret:+.2f}% hit={hit}")

def main():
    score_horizon(1, "scored_at_1d", "exit_price_1d", "return_1d_pct", "hit_1d")
    score_horizon(5, "scored_at_5d", "exit_price_5d", "return_5d_pct", "hit_5d")

if __name__ == "__main__":
    main()
