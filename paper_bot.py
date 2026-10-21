import json
import os
import time
from datetime import datetime, timezone

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API = "https://api.dexscreener.com"
STATE_FILE = "state.json"

START_CASH = 25.0
POSITION_USD = 5.0
MAX_POSITIONS = 2
FEE = 0.03
TAKE_PROFIT = 0.50
STOP_LOSS = -0.20
MAX_HOLD_HOURS = 24
MIN_LIQUIDITY = 25_000
MIN_VOLUME_24H = 50_000
MIN_AGE_MIN = 60
REDIST_PERIOD = 3600


def get(path):
    r = requests.get(API + path, timeout=20)
    r.raise_for_status()
    return r.json()


def send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20,
        )
    except Exception as e:
        print("telegram error", type(e).__name__)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def liq(p):
    return num((p.get("liquidity") or {}).get("usd"))


def vol(p, k):
    return num((p.get("volume") or {}).get(k))


def chg(p, k):
    return num((p.get("priceChange") or {}).get(k))


def txns(p, k):
    t = (p.get("txns") or {}).get(k) or {}
    return num(t.get("buys")), num(t.get("sells"))


def age_min(p):
    created = num(p.get("pairCreatedAt"))
    return (time.time() * 1000 - created) / 60000 if created else 0


def candidate_addresses():
    addrs = []
    for path in ("/token-boosts/latest/v1", "/token-profiles/latest/v1"):
        try:
            for item in get(path):
                if item.get("chainId") == "solana":
                    addrs.append(item["tokenAddress"])
        except Exception as e:
            print("candidate error", path, type(e).__name__)
    return list(dict.fromkeys(addrs))


def fetch_pairs(addrs):
    best = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        try:
            data = get("/latest/dex/tokens/" + ",".join(chunk))
        except Exception as e:
            print("pairs error", type(e).__name__)
            continue
        for p in data.get("pairs") or []:
            if p.get("chainId") != "solana" or not p.get("priceUsd"):
                continue
            a = p["baseToken"]["address"]
            if a not in best or liq(p) > liq(best[a]):
                best[a] = p
        time.sleep(0.3)
    return best


def momentum(p):
    b, s = txns(p, "h1")
    return 10 <= chg(p, "h1") <= 80 and b > s * 1.2


def dip_buyer(p):
    return chg(p, "h24") > 20 and -15 <= chg(p, "h1") <= -3


def volume_spike(p):
    avg = vol(p, "h24") / 24
    return avg > 0 and vol(p, "h1") > 3 * avg and chg(p, "h1") > 0


def breakout(p):
    avg = vol(p, "h24") / 24
    return (
        avg > 0
        and 5 <= chg(p, "h1") <= 60
        and vol(p, "h1") > avg
        and chg(p, "h24") < 30
    )


AGENTS = {
    "momentum": momentum,
    "dip_buyer": dip_buyer,
    "volume_spike": volume_spike,
    "breakout": breakout,
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {"last_report": "", "agents": {}}

    state.setdefault("last_redistribution", 0)
    for n in AGENTS:
        state["agents"].setdefault(
            n, {"cash": START_CASH, "positions": {}, "closed": []}
        )
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def equity(ag):
    return ag["cash"] + sum(
        p["qty"] * p["last_price"] for p in ag["positions"].values()
    )


def redistribute(state, pairs, now):
    last = state.get("last_redistribution", 0)
    if last == 0:
        state["last_redistribution"] = now
        return None
    if now - last < REDIST_PERIOD:
        return None

    equities = {n: equity(ag) for n, ag in state["agents"].items()}
    worst_name = min(equities, key=equities.get)
    best_name = max(equities, key=equities.get)
    if worst_name == best_name:
        state["last_redistribution"] = now
        return None

    worst = state["agents"][worst_name]

    # Liquidate the worst trader at current prices so "half capital" is unambiguous.
    for addr in list(worst["positions"]):
        pos = worst["positions"][addr]
        p = pairs.get(addr)
        if p:
            pos["last_price"] = num(p["priceUsd"])
        proceeds = pos["qty"] * pos["last_price"] * (1 - FEE)
        worst["cash"] += proceeds
        pnl = proceeds - pos["cost"]
        worst["closed"].append({"symbol": pos["symbol"], "pnl": round(pnl, 2)})
        worst["closed"] = worst["closed"][-500:]
        del worst["positions"][addr]

    transfer = worst["cash"] * 0.5
    worst["cash"] -= transfer
    state["agents"][best_name]["cash"] += transfer
    state["last_redistribution"] = now

    return (
        f"🔄 Hourly redistribution: {worst_name} → {best_name}, "
        f"transferred £{transfer:.2f} "
        f"(worst now £{worst['cash']:.2f}, "
        f"best now £{equity(state['agents'][best_name]):.2f})"
    )


def main():
    state = load_state()
    now = time.time()
    held = {a for ag in state["agents"].values() for a in ag["positions"]}
    cands = candidate_addresses()
    pairs = fetch_pairs(list(held | set(cands)))
    alerts = []

    redist = redistribute(state, pairs, now)
    if redist:
        alerts.append(redist)

    for name, rule in AGENTS.items():
        ag = state["agents"][name]

        for addr in list(ag["positions"]):
            pos = ag["positions"][addr]
            p = pairs.get(addr)
            if p:
                pos["last_price"] = num(p["priceUsd"])
            ret = pos["last_price"] / pos["entry_price"] - 1
            hours = (now - pos["opened_at"]) / 3600
            reason = None
            if ret >= TAKE_PROFIT:
                reason = "take profit"
            elif ret <= STOP_LOSS:
                reason = "stop loss"
            elif hours >= MAX_HOLD_HOURS:
                reason = "time limit"
            if reason:
                proceeds = pos["qty"] * pos["last_price"] * (1 - FEE)
                pnl = proceeds - pos["cost"]
                ag["cash"] += proceeds
                ag["closed"].append({"symbol": pos["symbol"], "pnl": round(pnl, 2)})
                ag["closed"] = ag["closed"][-500:]
                del ag["positions"][addr]
                alerts.append(
                    f"[{name}] SELL {pos['symbol']} ({reason}) "
                    f"{ret * 100:+.1f}%, P&L £{pnl:+.2f}"
                )

        for addr in cands:
            if len(ag["positions"]) >= MAX_POSITIONS or ag["cash"] < POSITION_USD:
                break
            p = pairs.get(addr)
            if not p or addr in ag["positions"]:
                continue
            if (
                liq(p) < MIN_LIQUIDITY
                or vol(p, "h24") < MIN_VOLUME_24H
                or age_min(p) < MIN_AGE_MIN
            ):
                continue
            if not rule(p):
                continue
            price = num(p["priceUsd"])
            if price <= 0:
                continue
            sym = p["baseToken"].get("symbol", "?")
            ag["cash"] -= POSITION_USD
            ag["positions"][addr] = {
                "symbol": sym,
                "entry_price": price,
                "last_price": price,
                "qty": POSITION_USD * (1 - FEE) / price,
                "cost": POSITION_USD,
                "opened_at": now,
            }
            alerts.append(f"[{name}] BUY {sym} at ${price:.8g} (£{POSITION_USD:.0f})")

    utc = datetime.now(timezone.utc)
    today = utc.strftime("%Y-%m-%d")
    if state["last_report"] != today and utc.hour >= 7:
        lines = ["Paper leaderboard:"]
        ranked = sorted(state["agents"].items(), key=lambda kv: -equity(kv[1]))
        for name, ag in ranked:
            eq = equity(ag)
            c = ag["closed"]
            wins = sum(1 for t in c if t["pnl"] > 0)
            wr = f"{wins / len(c) * 100:.0f}%" if c else "n/a"
            lines.append(
                f"{name}: £{eq:,.2f} ({(eq / START_CASH - 1) * 100:+.1f}%), "
                f"{len(c)} trades, win rate {wr}, open {len(ag['positions'])}"
            )
        alerts.append("\n".join(lines))
        state["last_report"] = today

    for a in alerts:
        send(a)
    save_state(state)


main()
