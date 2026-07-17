"""Core paper-trading engine.

Feeds (in fallback order): Kraken -> Coinbase Exchange -> Binance.
Kraken is primary because GitHub Actions runners are US-based and
Binance geo-blocks US IPs (HTTP 451).

All signals fire on CLOSED 1-minute candles. Intrabar exits use the
bar's high/low with stop-before-target (conservative) fill logic.
"""
import time
import requests

MARKETS = {
    "BTC": {"kraken": "XBTUSD", "coinbase": "BTC-USD", "binance": "BTCUSDT"},
    "ETH": {"kraken": "ETHUSD", "coinbase": "ETH-USD", "binance": "ETHUSDT"},
    "SOL": {"kraken": "SOLUSD", "coinbase": "SOL-USD", "binance": "SOLUSDT"},
}
# Full bid-ask spread as % of price (IC Markets-style crypto CFD quotes,
# commission zero). Entries/exits pay half the spread each side.
SPREAD_PCT = {"BTC": 0.02, "ETH": 0.16, "SOL": 0.25}
MK = list(MARKETS.keys())
START_BAL = 2000.0
REVIEW_EVERY = 8
MAX_CANDLES_KEPT = 300  # per market, enough for SMA(200)+

UA = {"User-Agent": "autobot-paper/1.0"}


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_day(t_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t_ms / 1000))


# ---------------- data feeds ----------------
def _kraken(mkt: str, since_ms: int):
    pair = MARKETS[mkt]["kraken"]
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": pair, "interval": 1, "since": max(0, since_ms // 1000)},
        headers=UA, timeout=20,
    )
    r.raise_for_status()
    res = r.json()["result"]
    rows = next(v for k, v in res.items() if isinstance(v, list))
    return [
        {"t": int(k[0]) * 1000, "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4])}
        for k in rows
    ]


def _coinbase(mkt: str, since_ms: int):
    product = MARKETS[mkt]["coinbase"]
    out, start, end = [], since_ms, now_ms()
    while start < end and len(out) < 2000:
        chunk_end = min(start + 300 * 60_000, end)
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={
                "granularity": 60,
                "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start / 1000)),
                "end": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(chunk_end / 1000)),
            },
            headers=UA, timeout=20,
        )
        r.raise_for_status()
        # rows: [time, low, high, open, close, volume], newest first
        for k in r.json():
            out.append({"t": int(k[0]) * 1000, "o": float(k[3]), "h": float(k[2]),
                        "l": float(k[1]), "c": float(k[4])})
        start = chunk_end + 60_000
    return out


def _binance(mkt: str, since_ms: int):
    sym = MARKETS[mkt]["binance"]
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": sym, "interval": "1m", "limit": 1000, "startTime": since_ms},
        headers=UA, timeout=20,
    )
    r.raise_for_status()
    return [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4])}
        for k in r.json()
    ]


def get_closed_candles(mkt: str, since_ms: int):
    """New closed candles strictly after since_ms, oldest first."""
    for fn in (_kraken, _coinbase, _binance):
        try:
            rows = fn(mkt, since_ms)
            cutoff = now_ms()
            rows = [b for b in rows if b["t"] > since_ms and b["t"] + 60_000 <= cutoff]
            rows.sort(key=lambda b: b["t"])
            if rows:
                return rows
        except Exception:
            continue
    return []


# ---------------- indicators ----------------
def sma(a, p):
    return sum(a[-p:]) / p if len(a) >= p else None


def rsi(closes, p=14):
    if len(closes) < p + 1:
        return None
    g = l = 0.0
    for i in range(len(closes) - p, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def momentum(a, p):
    return (a[-1] / a[-1 - p] - 1) * 100 if len(a) > p else None


def eval_conditions(conds, closes):
    if not isinstance(conds, list) or not conds:
        return False

    def cmp(x, op, v):
        return x < v if op == "<" else x > v

    for c in conds:
        try:
            ind = c.get("indicator")
            if ind == "rsi":
                r = rsi(closes, int(c.get("period", 14)))
                ok = r is not None and cmp(r, c.get("op", "<"), float(c["value"]))
            elif ind == "sma_cross":
                f = sma(closes, int(c.get("fast", 9)))
                s = sma(closes, int(c.get("slow", 21)))
                ok = f is not None and s is not None and (
                    f < s if c.get("state") == "bearish" else f > s)
            elif ind == "momentum":
                m = momentum(closes, int(c.get("period", 10)))
                ok = m is not None and cmp(m, c.get("op", ">"), float(c["value"]))
            elif ind == "price_vs_sma":
                s = sma(closes, int(c.get("period", 50)))
                ok = s is not None and cmp(closes[-1], c.get("op", ">"), s)
            else:
                ok = False
        except Exception:
            ok = False
        if not ok:
            return False
    return True


# ---------------- state ----------------
def fresh_state():
    t = now_ms()
    return {
        "balance": START_BAL,
        "equity": START_BAL,
        "equity_hist": [[t, START_BAL]],
        "candles": {m: [] for m in MK},
        "price": {m: None for m in MK},
        "last_seen": {m: 0 for m in MK},
        "positions": [],
        "trades": [],
        "strategy": None,
        "history": [],
        "log": [],
        "trades_since_review": 0,
        "day_anchor": {"day": utc_day(t), "eq": START_BAL},
        "halted": False,
        "cooldown": {},
        "seq": 1,
    }


def log(st, msg, kind="info", t=None):
    st["log"] = ([{"t": t or now_ms(), "msg": msg, "kind": kind}] + st["log"])[:120]


# ---------------- trading ----------------
def _fill(mkt, side, mid, is_entry):
    """Convert a mid price into a realistic fill: longs buy the ask and
    sell the bid; shorts the reverse. Half the spread each side."""
    half = SPREAD_PCT.get(mkt, 0.1) / 100 / 2
    if side == "long":
        return mid * (1 + half) if is_entry else mid * (1 - half)
    return mid * (1 - half) if is_entry else mid * (1 + half)


def _close(st, pos, price, reason, t):
    fill_px = _fill(pos["market"], pos["side"], price, False)
    pnl = (fill_px - pos["entry"] if pos["side"] == "long"
           else pos["entry"] - fill_px) * pos["units"]
    st["balance"] += pnl
    spread_cost = pos["units"] * price * SPREAD_PCT.get(pos["market"], 0.1) / 100
    st["trades"] = ([{
        "id": pos["id"], "market": pos["market"], "side": pos["side"],
        "entry": pos["entry"], "exit": fill_px, "units": pos["units"],
        "pnl": round(pnl, 2), "cost": round(spread_cost, 2),
        "t_in": pos["t_in"], "t_out": t,
        "held_min": round((t - pos["t_in"]) / 60_000),
        "exit_reason": reason, "strategy_version": pos["strategy_version"],
    }] + st["trades"])[:500]
    st["positions"] = [p for p in st["positions"] if p["id"] != pos["id"]]
    cd = (st["strategy"] or {}).get("risk", {}).get("cooldownMinutes", 15)
    st["cooldown"][pos["market"]] = t + cd * 60_000
    st["trades_since_review"] += 1
    log(st, f"{reason.upper()} {pos['market']} {pos['side']} -> "
            f"{'+' if pnl >= 0 else ''}${pnl:.2f}",
        "win" if pnl >= 0 else "loss", t)


def _open(st, mkt, side, price, t):
    r = st["strategy"]["risk"]
    entry_px = _fill(mkt, side, price, True)  # pay half-spread on entry
    risk_usd = st["equity"] * (r["riskPerTradePct"] / 100)
    units = risk_usd / (entry_px * (r["stopLossPct"] / 100))
    units = min(units, st["equity"] * 5 / entry_px)  # 5x notional cap
    sl = entry_px * (1 - r["stopLossPct"] / 100) if side == "long" \
        else entry_px * (1 + r["stopLossPct"] / 100)
    tp = entry_px * (1 + r["takeProfitPct"] / 100) if side == "long" \
        else entry_px * (1 - r["takeProfitPct"] / 100)
    st["seq"] += 1
    st["positions"].append({
        "id": st["seq"], "market": mkt, "side": side, "entry": entry_px,
        "units": units, "sl": sl, "tp": tp,
        "trail": r.get("trailingStopPct", 0) or 0, "best": entry_px,
        "t_in": t, "strategy_version": st["strategy"]["version"],
        "risk_usd": round(risk_usd, 2),
    })
    log(st, f"ENTER {mkt} {side} @ {entry_px:.2f} (incl. spread) | "
            f"SL {sl:.2f} TP {tp:.2f} | risk ${risk_usd:.2f}", "trade", t)


def _mark_equity(st, t, throttle_ms=10 * 60_000):
    open_pnl = 0.0
    for p in st["positions"]:
        px = st["price"].get(p["market"]) or p["entry"]
        open_pnl += (px - p["entry"] if p["side"] == "long"
                     else p["entry"] - px) * p["units"]
    st["equity"] = st["balance"] + open_pnl
    hist = st["equity_hist"]
    if not hist or t - hist[-1][0] > throttle_ms:
        hist.append([t, round(st["equity"], 2)])
        st["equity_hist"] = hist[-800:]


def _daily_guard(st, t):
    d = utc_day(t)
    if d != st["day_anchor"]["day"]:
        st["day_anchor"] = {"day": d, "eq": st["equity"]}
        if st["halted"]:
            st["halted"] = False
            log(st, "New UTC day - daily halt lifted.", "ai", t)
    dl = (st["strategy"] or {}).get("risk", {}).get("maxDailyLossPct", 3)
    if not st["halted"] and st["equity"] <= st["day_anchor"]["eq"] * (1 - dl / 100):
        st["halted"] = True
        for pos in list(st["positions"]):
            _close(st, pos, st["price"].get(pos["market"]) or pos["entry"], "halt", t)
        log(st, f"Daily loss limit {dl}% hit - flat until next UTC day.", "loss", t)


def process_bar(st, mkt, bar):
    """One closed 1-minute bar: exits, equity, guard, then entries."""
    t = bar["t"]
    st["price"][mkt] = bar["c"]
    st["candles"][mkt] = (st["candles"][mkt] + [bar])[-MAX_CANDLES_KEPT:]
    st["last_seen"][mkt] = max(st["last_seen"].get(mkt, 0), t)

    pos = next((p for p in st["positions"] if p["market"] == mkt), None)
    if pos:
        if pos["trail"] > 0:
            if pos["side"] == "long" and bar["h"] > pos["best"]:
                pos["best"] = bar["h"]
                pos["sl"] = max(pos["sl"], bar["h"] * (1 - pos["trail"] / 100))
            if pos["side"] == "short" and bar["l"] < pos["best"]:
                pos["best"] = bar["l"]
                pos["sl"] = min(pos["sl"], bar["l"] * (1 + pos["trail"] / 100))
        hit_sl = bar["l"] <= pos["sl"] if pos["side"] == "long" else bar["h"] >= pos["sl"]
        hit_tp = bar["h"] >= pos["tp"] if pos["side"] == "long" else bar["l"] <= pos["tp"]
        max_hold = (st["strategy"] or {}).get("risk", {}).get("maxHoldMinutes", 120)
        time_up = t - pos["t_in"] > max_hold * 60_000
        trail_won = pos["trail"] > 0 and (
            (pos["side"] == "long" and pos["sl"] > pos["entry"]) or
            (pos["side"] == "short" and pos["sl"] < pos["entry"]))
        if hit_sl:
            _close(st, pos, pos["sl"], "trail" if trail_won else "stop", t)
        elif hit_tp:
            _close(st, pos, pos["tp"], "target", t)
        elif time_up:
            _close(st, pos, bar["c"], "time", t)

    _mark_equity(st, t)
    _daily_guard(st, t)

    strat = st["strategy"]
    if not strat or st["halted"] or mkt not in strat["markets"]:
        return
    if len(st["positions"]) >= strat["risk"]["maxOpenPositions"]:
        return
    if any(p["market"] == mkt for p in st["positions"]):
        return
    if st["cooldown"].get(mkt, 0) > t:
        return
    closes = [b["c"] for b in st["candles"][mkt]]
    if len(closes) < 55:
        return
    if eval_conditions(strat["longConditions"], closes):
        _open(st, mkt, "long", bar["c"], t)
    elif eval_conditions(strat["shortConditions"], closes):
        _open(st, mkt, "short", bar["c"], t)


def stats(trades):
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    return {
        "n": len(trades),
        "win_rate": 100 * len(wins) / len(trades) if trades else 0,
        "pf": (gw / gl) if gl > 0 else (99 if gw > 0 else 0),
        "avg_win": gw / len(wins) if wins else 0,
        "avg_loss": gl / len(losses) if losses else 0,
        "net": gw - gl,
    }
