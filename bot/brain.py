"""The Claude brain v3: picks its own timeframe and markets from a
10-instrument universe with real spread costs, designs all parameters,
and revises everything in self-reviews. Hard risk caps always apply."""
import json
import os
import requests

from engine import (MARKETS, MK, TF_MIN, DEFAULT_TF, rsi, momentum, stats,
                    log, now_ms, START_BAL)

MODEL = os.environ.get("BOT_MODEL", "claude-sonnet-4-6")

SPREAD_TABLE = ", ".join(
    f"{m} {MARKETS[m]['spread']}%" for m in MK)

MARKET_NOTES = (
    "Round-trip spread cost per market: " + SPREAD_TABLE + ". "
    "BTC/ETH/SOL trade 24/7. XAU (gold), USTEC (Nasdaq 100) and all FX "
    "pairs are CLOSED on weekends - no candles then, positions can gap. "
    "USTEC and XAU have the cheapest spreads; ETH/SOL the most expensive. "
    "Typical realistic move sizes per candle differ by market - size "
    "stops/targets to the instrument and timeframe, and make sure the "
    "expected win comfortably exceeds the spread."
)

SCHEMA_TEXT = """{
 "name": string,
 "timeframe": "5m" | "15m" | "1h",
 "markets": subset (max 4) of """ + json.dumps(MK) + """,
 "rationale": string (<=40 words),
 "longConditions": [cond,...],
 "shortConditions": [cond,...],
 "risk": {"riskPerTradePct":number,"stopLossPct":number,"takeProfitPct":number,"trailingStopPct":number,"maxHoldMinutes":number,"maxOpenPositions":number,"maxDailyLossPct":number,"cooldownMinutes":number}
}
cond is ONE of:
 {"indicator":"rsi","period":int,"op":"<"|">","value":number}
 {"indicator":"sma_cross","fast":int,"slow":int,"state":"bullish"|"bearish"}
 {"indicator":"momentum","period":int,"op":"<"|">","value":number}
 {"indicator":"price_vs_sma","period":int,"op":"<"|">"}"""


def _clamp(v, lo, hi, d):
    try:
        v = float(v)
        if v != v or v in (float("inf"), float("-inf")):
            return d
        return min(hi, max(lo, v))
    except (TypeError, ValueError):
        return d


def sanitize(s, version):
    if not isinstance(s, dict):
        return None
    r = s.get("risk") or {}
    mkts = [m for m in (s.get("markets") or []) if m in MK][:4] or ["BTC"]
    tf = s.get("timeframe")
    if tf not in TF_MIN:
        tf = DEFAULT_TF
    return {
        "name": str(s.get("name", "Unnamed"))[:60],
        "version": version,
        "timeframe": tf,
        "markets": mkts,
        "rationale": str(s.get("rationale", ""))[:300],
        "longConditions": (s.get("longConditions") or [])[:5],
        "shortConditions": (s.get("shortConditions") or [])[:5],
        "risk": {
            "riskPerTradePct": _clamp(r.get("riskPerTradePct"), 0.1, 2, 0.75),
            "stopLossPct": _clamp(r.get("stopLossPct"), 0.15, 5, 0.8),
            "takeProfitPct": _clamp(r.get("takeProfitPct"), 0.2, 10, 1.6),
            "trailingStopPct": _clamp(r.get("trailingStopPct"), 0, 5, 0),
            "maxHoldMinutes": _clamp(r.get("maxHoldMinutes"), 15, 2880, 360),
            "maxOpenPositions": int(_clamp(r.get("maxOpenPositions"), 1, 3, 2)),
            "maxDailyLossPct": _clamp(r.get("maxDailyLossPct"), 0.5, 3, 3),
            "cooldownMinutes": _clamp(r.get("cooldownMinutes"), 0, 720, 30),
        },
    }


FALLBACK = sanitize({
    "name": "Bootstrap trend-follow (offline fallback)",
    "timeframe": "15m",
    "markets": ["BTC", "XAU"],
    "rationale": "Used because the strategy API call failed. SMA trend with RSI pullback entries on cheap-spread markets.",
    "longConditions": [
        {"indicator": "sma_cross", "fast": 9, "slow": 21, "state": "bullish"},
        {"indicator": "rsi", "period": 14, "op": "<", "value": 55},
    ],
    "shortConditions": [
        {"indicator": "sma_cross", "fast": 9, "slow": 21, "state": "bearish"},
        {"indicator": "rsi", "period": 14, "op": ">", "value": 45},
    ],
    "risk": {"riskPerTradePct": 0.75, "stopLossPct": 0.8, "takeProfitPct": 1.6,
             "trailingStopPct": 0, "maxHoldMinutes": 720, "maxOpenPositions": 2,
             "maxDailyLossPct": 3, "cooldownMinutes": 60},
}, 1)


def ask_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        for base in (os.path.dirname(os.path.abspath(__file__)),
                     os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            p = os.path.join(base, "api_key.txt")
            if os.path.exists(p):
                with open(p) as f:
                    key = f.read().strip()
                break
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 1400,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"]
                   if b.get("type") == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def _valid(strat):
    return strat and (strat["longConditions"] or strat["shortConditions"])


def _snapshot(st):
    lines = []
    for m in MK:
        c = [b["c"] for b in st["candles"][m]]
        if not c:
            lines.append(f"{m}: closed/no data")
            continue
        line = f"{m}: {c[-1]:.5g}"
        r = rsi(c)
        mo = momentum(c, 20)
        if r is not None:
            line += f", RSI14 {r:.1f}"
        if mo is not None:
            line += f", mom20 {mo:.2f}%"
        lines.append(line)
    return "; ".join(lines)


def bootstrap(st):
    prompt = (
        f"You are the autonomous strategy brain of a ${START_BAL:.0f} "
        f"paper-trading account executed by a cron job every ~5-15 minutes "
        f"(signals on closed candles only). Choose your own timeframe "
        f"(5m/15m/1h), your own markets (max 4), and every risk parameter. "
        f"{MARKET_NOTES} Be selective - quality over quantity - and "
        f"conservative on a small account. Current snapshot (15m candles): "
        f"{_snapshot(st)}. "
        f"Respond ONLY with raw JSON, no markdown, matching:\n{SCHEMA_TEXT}"
    )
    try:
        strat = sanitize(ask_claude(prompt), 1)
        if not _valid(strat):
            raise ValueError("empty conditions")
        st["strategy"] = strat
        st["history"] = [{"version": 1, "t": now_ms(),
                          "analysis": strat["rationale"],
                          "changes": [f"Initial strategy: {strat['timeframe']} timeframe, "
                                      f"markets {', '.join(strat['markets'])}"],
                          "strategy": strat}]
        log(st, f'Strategy v1 online: "{strat["name"]}" | {strat["timeframe"]} | '
                f'{", ".join(strat["markets"])}', "ai")
    except Exception as e:
        st["strategy"] = FALLBACK
        st["history"] = [{"version": 1, "t": now_ms(),
                          "analysis": FALLBACK["rationale"],
                          "changes": [f"Fallback strategy (API unavailable: {type(e).__name__})"],
                          "strategy": FALLBACK}]
        log(st, "Strategy API unavailable - running built-in fallback.", "warn")


def review(st):
    if not st["strategy"] or not st["trades"]:
        st["trades_since_review"] = 0
        return
    s = stats(st["trades"])
    recent = "\n".join(
        f'{t["market"]} {t["side"]} {t["entry"]:.5g}->{t["exit"]:.5g} '
        f'pnl {t["pnl"]:.2f} cost {t.get("cost", 0):.2f} '
        f'({t["exit_reason"]}, {t["held_min"]}m, v{t["strategy_version"]})'
        for t in st["trades"][:20])
    by_reason = {}
    total_cost = 0.0
    for t in st["trades"]:
        by_reason[t["exit_reason"]] = round(
            by_reason.get(t["exit_reason"], 0) + t["pnl"], 2)
        total_cost += t.get("cost", 0)
    cur = dict(st["strategy"])
    cur.pop("version", None)
    prompt = (
        f"You are the self-learning brain of a ${START_BAL:.0f} paper "
        f"account (cron-executed, closed-candle signals). {MARKET_NOTES} "
        f"Review your own journal, name your mistakes, and output an "
        f"IMPROVED strategy. You may change the timeframe, markets, "
        f"indicators, thresholds, R:R, sizing, hold time, or simply trade "
        f"less. Never increase risk after losses.\n\n"
        f"Stats: {s['n']} trades, WR {s['win_rate']:.1f}%, PF {s['pf']:.2f}, "
        f"avgW ${s['avg_win']:.2f}, avgL ${s['avg_loss']:.2f}, "
        f"net ${s['net']:.2f}, total spread paid ${total_cost:.2f}, "
        f"equity ${st['equity']:.2f}. PnL by exit: {json.dumps(by_reason)}.\n"
        f"Current v{st['strategy']['version']}: {json.dumps(cur)}\n"
        f"Recent trades:\n{recent}\n\n"
        f'Respond ONLY raw JSON: {{"analysis":string(<=50 words),'
        f'"mistakes":[...],"changes":[...],"strategy":<schema>}}\n{SCHEMA_TEXT}'
    )
    try:
        out = ask_claude(prompt)
        strat = sanitize(out.get("strategy"), st["strategy"]["version"] + 1)
        if not _valid(strat):
            raise ValueError("empty conditions")
        st["strategy"] = strat
        st["history"] = ([{
            "version": strat["version"], "t": now_ms(),
            "analysis": str(out.get("analysis", ""))[:400],
            "mistakes": [str(x) for x in (out.get("mistakes") or [])][:4],
            "changes": [str(x) for x in (out.get("changes") or [])][:5],
            "strategy": strat,
        }] + st["history"])[:20]
        log(st, f"Self-review done -> strategy v{strat['version']} deployed "
                f"({strat['timeframe']}, {', '.join(strat['markets'])})", "ai")
    except Exception:
        log(st, "Self-review call failed - keeping current strategy.", "warn")
    st["trades_since_review"] = 0
