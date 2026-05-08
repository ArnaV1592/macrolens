# report.py — runs Sundays 14:30 UTC (8:00 PM IST)
import os, httpx, psycopg2, psycopg2.extras
from contextlib import contextmanager

PG_DSN = os.environ["SUPABASE_PG_DSN"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT  = os.environ["TG_CHAT_ID"]

@contextmanager
def cur():
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cu:
            yield cu
    finally:
        conn.close()

def fetch_stats():
    with cur() as c:
        c.execute("""
          SELECT s.detector,
                 COUNT(*) FILTER (WHERE o.hit_1d IS NOT NULL) AS scored_1d,
                 AVG(CASE WHEN o.hit_1d THEN 1.0 ELSE 0.0 END) FILTER (WHERE o.hit_1d IS NOT NULL) AS hit_rate_1d,
                 AVG(o.return_1d_pct) FILTER (WHERE o.return_1d_pct IS NOT NULL) AS avg_ret_1d,
                 COUNT(*) FILTER (WHERE o.hit_5d IS NOT NULL) AS scored_5d,
                 AVG(CASE WHEN o.hit_5d THEN 1.0 ELSE 0.0 END) FILTER (WHERE o.hit_5d IS NOT NULL) AS hit_rate_5d,
                 AVG(o.return_5d_pct) FILTER (WHERE o.return_5d_pct IS NOT NULL) AS avg_ret_5d
          FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id = s.id
          GROUP BY s.detector ORDER BY s.detector
        """)
        return c.fetchall()

def main():
    rows = fetch_stats()
    if not rows:
        body = "_No signals tracked yet._"
    else:
        lines = []
        for r in rows:
            lines.append(
                f"*{r['detector']}*\n"
                f"  1-day: {int(r['scored_1d'] or 0)} scored, "
                f"hit={float(r['hit_rate_1d'] or 0)*100:.0f}%, "
                f"avg ret={float(r['avg_ret_1d'] or 0):+.2f}%\n"
                f"  5-day: {int(r['scored_5d'] or 0)} scored, "
                f"hit={float(r['hit_rate_5d'] or 0)*100:.0f}%, "
                f"avg ret={float(r['avg_ret_5d'] or 0):+.2f}%"
            )
        body = "\n\n".join(lines)
    msg = f"📊 *Weekly macrolens report*\n\n{body}\n\n_Capital still on paper. Ship discipline > ship trades._"
    httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
               json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=15)

if __name__ == "__main__":
    main()
