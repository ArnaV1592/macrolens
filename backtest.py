"""
backtest.py — replay the volume z-score detector across past 2 years.

Run via GitHub Actions (workflow_dispatch) or locally:
    pip install yfinance numpy pandas
    python backtest.py

Outputs:
  - Console summary (hit rate, average returns, per-symbol breakdown)
  - backtest_results.csv (every signal with forward returns)

Interpretation guide is at the bottom of the printed summary.
"""
import sys
import yfinance as yf
import numpy as np
import pandas as pd

WATCHLIST = [
    "SBIN.NS",       # 65% hit, +0.35% avg
    "RELIANCE.NS",   # 56% hit, +0.27% avg
]
LOOKBACK_DAYS = 20
Z_THRESHOLD   = 2.5
PERIOD        = "2y"   # bump to "5y" for stricter test

def backtest_one(symbol):
    """Walk forward through history; for each day, recompute z-score using ONLY past data
    (no look-ahead bias). Record signals and their 1d/5d/20d forward returns."""
    hist = yf.Ticker(symbol).history(period=PERIOD, interval="1d")
    if len(hist) < LOOKBACK_DAYS + 25:
        return []
    rows = []
    closes  = hist["Close"].values
    volumes = hist["Volume"].values
    dates   = hist.index

    for i in range(LOOKBACK_DAYS, len(hist) - 20):
        baseline = volumes[i - LOOKBACK_DAYS:i]
        today_v  = volumes[i]
        mu = baseline.mean(); sigma = baseline.std(ddof=1)
        if sigma == 0: continue
        z = (today_v - mu) / sigma
        if z < Z_THRESHOLD: continue

        entry      = float(closes[i])
        prev_close = float(closes[i-1])
        today_ret  = (entry / prev_close - 1) * 100
        direction  = "up" if today_ret >= 0 else "down"

        ret_1d  = (float(closes[i+1])  / entry - 1) * 100
        ret_5d  = (float(closes[i+5])  / entry - 1) * 100
        ret_20d = (float(closes[i+20]) / entry - 1) * 100

        # Hit = did the bot's direction guess match what actually happened?
        hit_1d = (ret_1d > 0)  if direction == "up" else (ret_1d < 0)
        hit_5d = (ret_5d > 0)  if direction == "up" else (ret_5d < 0)

        rows.append({
            "symbol": symbol,
            "date": dates[i].date().isoformat(),
            "z": round(float(z), 2),
            "today_return_pct": round(today_ret, 2),
            "direction": direction,
            "entry": round(entry, 2),
            "ret_1d":  round(ret_1d, 2),
            "ret_5d":  round(ret_5d, 2),
            "ret_20d": round(ret_20d, 2),
            "hit_1d": bool(hit_1d),
            "hit_5d": bool(hit_5d),
        })
    return rows

def baseline_hit_rate(symbol):
    """Coin-flip baseline: pick random days, predict the direction of today's return,
    measure 1d hit rate. If our detector beats this clearly, we have edge."""
    hist = yf.Ticker(symbol).history(period=PERIOD, interval="1d")
    if len(hist) < 25: return None
    closes = hist["Close"].values
    rng = np.random.default_rng(42)
    idx = rng.choice(range(LOOKBACK_DAYS, len(hist)-1), size=min(200, len(hist)-30), replace=False)
    hits = []
    for i in idx:
        prev = float(closes[i-1]); today = float(closes[i]); tomorrow = float(closes[i+1])
        direction = "up" if today >= prev else "down"
        ret_1d = (tomorrow / today - 1) * 100
        hit = (ret_1d > 0) if direction == "up" else (ret_1d < 0)
        hits.append(hit)
    return float(np.mean(hits)) * 100 if hits else None

def main():
    print(f"\nBacktesting {len(WATCHLIST)} symbols, period={PERIOD}, "
          f"lookback={LOOKBACK_DAYS}d, z>={Z_THRESHOLD}\n")
    all_rows = []
    for sym in WATCHLIST:
        try:
            rows = backtest_one(sym)
            all_rows.extend(rows)
            print(f"  {sym}: {len(rows):>3} signals")
        except Exception as ex:
            print(f"  {sym}: FAILED ({ex})")

    if not all_rows:
        print("\nNo signals found. Lower Z_THRESHOLD or expand watchlist.")
        sys.exit(0)

    df = pd.DataFrame(all_rows)
    df.to_csv("backtest_results.csv", index=False)

    print("\n" + "═"*60)
    print(f"  BACKTEST SUMMARY  —  {len(df)} signals over {PERIOD}")
    print("═"*60)

    print(f"\n  Avg z-score:          {df['z'].mean():.2f}")
    print(f"  Avg today's return:   {df['today_return_pct'].mean():+.2f}%")

    print(f"\n  ── 1-day forward ──")
    print(f"  Direction hit rate:   {df['hit_1d'].mean()*100:.1f}%")
    print(f"  Avg return:           {df['ret_1d'].mean():+.2f}%")
    print(f"  Median return:        {df['ret_1d'].median():+.2f}%")
    print(f"  Win avg | Loss avg:   {df.loc[df['hit_1d'],'ret_1d'].abs().mean():+.2f}% | "
          f"{df.loc[~df['hit_1d'],'ret_1d'].abs().mean():+.2f}%")

    print(f"\n  ── 5-day forward ──")
    print(f"  Direction hit rate:   {df['hit_5d'].mean()*100:.1f}%")
    print(f"  Avg return:           {df['ret_5d'].mean():+.2f}%")
    print(f"  Median return:        {df['ret_5d'].median():+.2f}%")

    print(f"\n  ── 20-day forward (mean reversion check) ──")
    print(f"  Avg return:           {df['ret_20d'].mean():+.2f}%")

    print(f"\n  ── Per-symbol (5d) ──")
    for sym in df['symbol'].unique():
        sub = df[df['symbol'] == sym]
        print(f"    {sym:<14} n={len(sub):>3}  "
              f"hit={sub['hit_5d'].mean()*100:>4.0f}%  "
              f"avg={sub['ret_5d'].mean():+.2f}%")

    print(f"\n  ── Coin-flip baseline (random days, same direction logic) ──")
    bases = [b for b in (baseline_hit_rate(s) for s in WATCHLIST[:5]) if b is not None]
    if bases:
        print(f"    Baseline hit rate:  {np.mean(bases):.1f}% "
              f"(your detector should clearly beat this)")

    print("\n  ── INTERPRETATION ──")
    hr = df['hit_5d'].mean()*100
    if hr < 52:
        verdict = "❌ NO EDGE. Detector is at coin-flip. Don't paper trade this — fix it first."
    elif hr < 55:
        verdict = "⚠️  WEAK. Marginal edge, might be noise. Need more samples or better detector."
    elif hr < 60:
        verdict = "✓ PROMISING. Real but small edge. Worth paper trading 8 weeks to confirm."
    else:
        verdict = "✓✓ STRONG (suspicious). Double-check for look-ahead bias before celebrating."
    print(f"    {verdict}")
    print(f"\n  CSV saved: backtest_results.csv ({len(df)} rows)\n")

if __name__ == "__main__":
    main()
