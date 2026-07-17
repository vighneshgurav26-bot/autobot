"""Entry point for each cron run.

1. Load state.json (or start a fresh $2,000 account)
2. Warm up / catch up: fetch every closed 1-min candle since last run
3. Replay them through the engine in time order (trades happen here)
4. Design or review the strategy via Claude when due
5. Save state.json + docs/data.json for the dashboard
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (MK, REVIEW_EVERY, fresh_state, get_closed_candles, log,
                    now_ms, process_bar, stats)
import brain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "state.json")
DATA_PATH = os.path.join(ROOT, "docs", "data.json")
WARMUP_MS = 6 * 3600 * 1000  # first ever run: 6h of history for indicators


def load_state():
    try:
        with open(STATE_PATH) as f:
            st = {**fresh_state(), **json.load(f)}
        return st, False
    except Exception:
        return fresh_state(), True


def main():
    st, first_run = load_state()
    if first_run:
        log(st, "Fresh $2,000 paper account initialised.")

    # ---- catch up on every market ----
    fetched = []
    for m in MK:
        since = st["last_seen"].get(m) or (now_ms() - WARMUP_MS)
        for bar in get_closed_candles(m, since):
            fetched.append((m, bar))
    fetched.sort(key=lambda x: x[1]["t"])

    feed_ok = bool(fetched)
    if not feed_ok:
        log(st, "No new candles this run (feeds unreachable or no gap).", "warn")

    if first_run and fetched:
        # warm indicators without trading on stale history:
        # replay all but the last 30 bars per market with strategy disabled
        counts = {}
        for m, _ in fetched:
            counts[m] = counts.get(m, 0) + 1
        seen = {}
        strategy_backup, st["strategy"] = st["strategy"], None
        live_tail = []
        for m, bar in fetched:
            seen[m] = seen.get(m, 0) + 1
            if seen[m] <= counts[m] - 30:
                process_bar(st, m, bar)
            else:
                live_tail.append((m, bar))
        st["strategy"] = strategy_backup
        brain.bootstrap(st)
        for m, bar in live_tail:
            process_bar(st, m, bar)
    else:
        if st["strategy"] is None:
            brain.bootstrap(st)
        for m, bar in fetched:
            process_bar(st, m, bar)

    # ---- self-review when due ----
    if st["trades_since_review"] >= REVIEW_EVERY:
        brain.review(st)

    # ---- persist full state ----
    with open(STATE_PATH, "w") as f:
        json.dump(st, f)

    # ---- dashboard payload (trimmed) ----
    s = stats(st["trades"])
    data = {
        "updated_at": now_ms(),
        "feed_ok": feed_ok,
        "start_balance": 2000,
        "balance": round(st["balance"], 2),
        "equity": round(st["equity"], 2),
        "day_anchor": st["day_anchor"],
        "halted": st["halted"],
        "price": st["price"],
        "equity_hist": st["equity_hist"][-700:],
        "positions": st["positions"],
        "trades": st["trades"][:120],
        "strategy": st["strategy"],
        "history": st["history"][:8],
        "log": st["log"][:60],
        "stats": {k: round(v, 2) for k, v in s.items()},
        "trades_since_review": st["trades_since_review"],
        "review_every": REVIEW_EVERY,
    }
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(data, f)

    print(f"OK | equity ${st['equity']:.2f} | {s['n']} trades | "
          f"{len(st['positions'])} open | strategy "
          f"v{st['strategy']['version'] if st['strategy'] else '-'}")


if __name__ == "__main__":
    main()
