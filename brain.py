"""The Claude brain: designs the strategy, reviews its own journal,
and ships improved versions. All numeric risk parameters are clamped
to hard caps regardless of what the model asks for."""
import json
import os
import requests

from engine import MK, rsi, momentum, stats, log, now_ms, START_BAL

MODEL = os.environ.get("BOT_MODEL", "claude-sonnet-4-6")

SCHEMA_TEXT = """{
 "name": string,
 "markets": subset of ["BTC","ETH","SOL"],
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
    mkts = [m for m in (s.get("markets") or []) if m in MK] or ["BTC"]
    return {
        "name": str(s.get("name", "Unnamed"))[:60],
        "version": version,
        "markets": mkts,
        "rationale": str(s.get("rationale", ""))[:300],
        "longConditions": (s.get("longConditions") or [])[:5],
        "shortConditions": (s.get("shortConditions") or [])[:5],
        "risk": {
            "riskPerTradePct": _clamp(r.get("riskPerTradePct"), 0.1, 2, 0.75),
            "stopLossPct": _clamp(r.get("stopLossPct"), 0.15, 4, 0.6),
            "takeProfitPct": _clamp(r.get("takeProfitPct"), 0.2, 8, 1.2),
            "trailingStopPct": _clamp(r.get("trailingStopPct"), 0, 4, 0),
            "maxHoldMinutes": _clamp(r.get("maxHoldMinutes"), 10, 720, 120),
            "maxOpenPositions": int(_clamp(r.get("maxOpenPositions"), 1, 3, 2)),
            "maxDailyLossPct": _clamp(r.get("maxDailyLossPct"), 0.5, 3, 3),
            "cooldownMinutes": _clamp(r.get("cooldownMinutes"), 0, 180, 15),
        },
    }


FALLBACK = sanitize({
    "name": "Bootstrap mean-revert (offline fallback)",
    "markets": ["BTC"],
    "rationale": "Used because the strategy API call failed. RSI dip-buy / rip-sell with SMA50 trend filter.",
    "longConditions": [
        {"indicator": "rsi", "period": 14, "op": "<", "value": 32},
        {"indicator": "price_vs_sma", "period": 50, "op": ">"},
    ],
    "shortConditions": [
        {"indicator": "rsi", "period": 14, "op": ">", "value": 70},
        {"indicator": "price_vs_sma", "period": 50, "op": "<"},
    ],
    "risk": {"riskPerTradePct": 0.75, "stopLossPct": 0.5, "takeProfitPct": 1.0,
             "trailingStopPct": 0, "maxHoldMinutes": 120, "maxOpenPositions": 1,
             "maxDailyLossPct": 3, "cooldownMinutes": 15},
}, 1)


def ask_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        # VPS convenience: read key from api_key.txt next to the bot files
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
        json={"model": MODEL, "max_tokens": 1200,
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


def bootstrap(st):
    snap = []
    for m in MK:
        c = [b["c"] for b in st["candles"][m]]
        px = f"{c[-1]:.2f}" if c else "n/a"
        r = rsi(c)
        mo = momentum(c, 60)
        line = f"{m}: {px}"
        if r is not None:
            line += f", RSI14 {r:.1f}"
        if mo is not None:
            line += f", 1h mom {mo:.2f}%"
        snap.append(line)
    prompt = (
        f"You are the autonomous strategy brain of a ${START_BAL:.0f} crypto "
        f"paper-trading account on 1-minute candles, executed by a cron job "
        f"every ~5 minutes (signals on closed candles). Fills pay realistic "
        f"spread costs: BTC ~0.02%, ETH ~0.16%, SOL ~0.25% round trip "
        f"(commission zero) - avoid setups whose edge is eaten by spread. "
        f"Design the initial "
        f"strategy AND every risk parameter yourself. Be selective - quality "
        f"over quantity - and conservative on a small account. "
        f"Market snapshot: {'; '.join(snap)}. "
        f"Respond ONLY with raw JSON, no markdown, matching:\n{SCHEMA_TEXT}"
    )
    try:
        strat = sanitize(ask_claude(prompt), 1)
        if not _valid(strat):
            raise ValueError("empty conditions")
        st["strategy"] = strat
        st["history"] = [{"version": 1, "t": now_ms(),
                          "analysis": strat["rationale"],
                          "changes": ["Initial strategy designed from live market snapshot"],
                          "strategy": strat}]
        log(st, f'Strategy v1 online: "{strat["name"]}" on {", ".join(strat["markets"])}', "ai")
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
        f'{t["market"]} {t["side"]} {t["entry"]:.2f}->{t["exit"]:.2f} '
        f'pnl {t["pnl"]:.2f} cost {t.get("cost", 0):.2f} '
        f'({t["exit_reason"]}, {t["held_min"]}m, v{t["strategy_version"]})'
        for t in st["trades"][:20])
    by_reason = {}
    for t in st["trades"]:
        by_reason[t["exit_reason"]] = round(
            by_reason.get(t["exit_reason"], 0) + t["pnl"], 2)
    cur = dict(st["strategy"])
    cur.pop("version", None)
    prompt = (
        f"You are the self-learning brain of a ${START_BAL:.0f} crypto paper "
        f"account (1-min candles, cron-executed). Every fill pays realistic "
        f"spread: BTC ~0.02%, ETH ~0.16%, SOL ~0.25% round trip. The cost "
        f"column in trades shows spread paid - overtrading and tight targets "
        f"bleed money. Review your own journal, "
        f"name your mistakes, and output an IMPROVED strategy. You may change "
        f"indicators, thresholds, markets, R:R, sizing, hold time, or simply "
        f"trade less. Never increase risk after losses.\n\n"
        f"Stats: {s['n']} trades, WR {s['win_rate']:.1f}%, PF {s['pf']:.2f}, "
        f"avgW ${s['avg_win']:.2f}, avgL ${s['avg_loss']:.2f}, "
        f"net ${s['net']:.2f}, equity ${st['equity']:.2f}. "
        f"PnL by exit: {json.dumps(by_reason)}.\n"
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
        log(st, f"Self-review done -> strategy v{strat['version']} deployed", "ai")
    except Exception:
        log(st, "Self-review call failed - keeping current strategy.", "warn")
    st["trades_since_review"] = 0
