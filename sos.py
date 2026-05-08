"""
sos.py — runs every 5 min via GitHub Actions, 24/7.
  1. Pull a small set of HIGH-PRIORITY RSS feeds (RBI, breaking-news macros)
  2. Regex-match crisis keywords
  3. LLM classifies: is this likely to move Indian markets in 24h?
  4. If yes/maybe → 🚨 push to Telegram (with sound, not silent)
"""
import os, re, hashlib, logging
import feedparser, httpx, psycopg2
from groq import Groq

PG_DSN   = os.environ["SUPABASE_PG_DSN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT  = os.environ["TG_CHAT_ID"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sos")

# Keep this list TIGHT. Bigger list = more noise = your SOS becomes a regular alert.
PRIORITY_FEEDS = [
    "https://www.rbi.org.in/Scripts/Rss.aspx?Cat=53",
    "https://news.google.com/rss/search?q=RBI+rate+OR+repo+OR+%22monetary+policy%22&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=SEBI+ban+OR+%22circuit+breaker%22+OR+%22trading+halt%22&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=India+sanctions+OR+tariffs+OR+border+OR+ceasefire&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Fed+rate+OR+FOMC+OR+%22interest+rate%22&hl=en-US&gl=US&ceid=US:en",
]

# Word-boundary regex — case-insensitive on lowered text.
# Keep keywords specific. Vague matches (e.g. just "war") flood you with noise.
SOS_PATTERNS = {
    "rbi_action":     r"\b(rbi|reserve bank)\b.{0,60}\b(rate|repo|cut|hike|emergency|liquidity|crr|slr)\b",
    "regulatory":     r"\b(sebi|nse|bse)\b.{0,40}\b(ban|halt|suspend|investigation|circuit|notice|raid)\b",
    "geopolitical":   r"\b(sanctions?|new tariffs?|airstrike|missile strike|border clash|ceasefire)\b",
    "macro_shock":    r"\b(inflation.{0,20}(spike|surge|jump)|gdp.{0,20}contract|recession|sovereign default|downgrade.{0,20}(india|sovereign))\b",
    "corporate_blow": r"\b(forensic audit|sebi notice|cbi raid|fraud charges?|insolvency|bankruptcy filing)\b",
    "fed_action":     r"\bfed\b.{0,40}\b(hike|cut|surprise|emergency|fomc decision)\b",
}

def _eid(*parts): return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

def already_alerted(key):
    with psycopg2.connect(PG_DSN) as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM alerts_sent WHERE key=%s", (key,))
            if c.fetchone():
                return True
            c.execute("INSERT INTO alerts_sent (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
            return False

def match_categories(text):
    t = text.lower()
    return [cat for cat, pat in SOS_PATTERNS.items() if re.search(pat, t, re.IGNORECASE)]

def llm_verdict(title, summary, categories):
    prompt = f"""You are a strict Indian-markets crisis filter. Many headlines look dramatic but DO NOT move markets. Be conservative.

Headline matched these regex categories: {categories}

Title: {title}
Summary: {(summary or '')[:500]}

Will this likely move NIFTY/BANKNIFTY/major-stock prices in the next 24 hours?
Default to "no" unless the news is concrete and immediate.

Reply EXACTLY 2 lines:
VERDICT: yes | no | maybe
WHY: <one short sentence>"""
    try:
        out = Groq(api_key=GROQ_KEY).chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=120,
        ).choices[0].message.content
        return out.strip()
    except Exception as ex:
        log.warning(f"LLM error: {ex}")
        return "VERDICT: maybe\nWHY: classifier unavailable, defaulting to push for manual review"

def push_sos(title, url, categories, verdict_text):
    msg = (
        f"🚨 *SOS — possible market event* 🚨\n\n"
        f"*{title}*\n\n"
        f"`{', '.join(categories)}`\n\n"
        f"```\n{verdict_text}\n```\n"
        f"🔗 {url}"
    )
    httpx.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_notification": False,  # SOS = sound on
        },
        timeout=15,
    )

def main():
    sent = 0
    for feed_url in PRIORITY_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for e in feed.entries[:30]:
                title = e.get("title", "")
                summary = e.get("summary", "")
                link = e.get("link", "")
                cats = match_categories(f"{title} {summary}")
                if not cats:
                    continue
                key = "sos:" + _eid(feed_url, link, title)
                if already_alerted(key):
                    continue
                verdict = llm_verdict(title, summary, cats)
                if verdict.upper().startswith("VERDICT: NO"):
                    log.info(f"LLM rejected: {title}")
                    continue
                push_sos(title, link, cats, verdict)
                sent += 1
                log.info(f"SOS sent: {title}")
        except Exception as ex:
            log.warning(f"feed {feed_url}: {ex}")
    log.info(f"sos cycle complete: {sent} alerts sent")

if __name__ == "__main__":
    main()
