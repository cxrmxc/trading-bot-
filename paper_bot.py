import json
import os
import time
from datetime import datetime, timezone

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API = "https://api.dexscreener.com"
STATE_FILE = "state.json"
STATE_VERSION = 2

START_CASH = 25.0
POSITION_FRACTION = 0.25
MAX_POSITIONS = 3
FEE = 0.03
TAKE_PROFIT = 0.50
STOP_LOSS = -0.20
MAX_HOLD_HOURS = 24
MIN_LIQUIDITY = 25_000
MIN_VOLUME_24H = 50_000
MIN_AGE_MIN = 60

TRANSFER_FRACTION = 0.5
REBALANCE_SECONDS = 3300


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


def fresh_launch(p):
    b, s = txns(p, "h1")
    return 60 <= age_min(p) <= 360 and b > s * 1.5 and chg(p, "h1") > 0


AGENTS = {
    "momentum": momentum,
    "dip_buyer": dip_buyer,
    "volume_spike": volume_spike,
    "fresh_launch": fresh_launch,
}


def new_state():
    return {
        "version": STATE_VERSION,
        "last_report": "",
        "last_rebalance": time.time(),
        "agents": {
            n: {
                "cash": START_CASH,
                "positions": {},
                "closed": [],
                "snapshot": START_CASH,
            }
            for n in AGENTS
        },
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("version") == STATE_VERSION:
            return s
    return new_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def equity(ag):
    return ag["cash"] + sum(
        p["qty"] * p["last_price"] for p in ag["positions"].values()
    )


def rebalance(state, now, alerts):
    if now - state["last_rebalance"] < REBALANCE_SECONDS:
        return
    perf = {}
    for name, ag in state["agents"].items():
        base = ag["snapshot"]
        perf[name] = equity(ag) / base - 1 if base > 0 else 0.0
    best = max(perf, key=perf.get)
    worst = min(perf, key=perf.get)
    if best != worst and perf[best] > perf[worst]:
        w = state["agents"][worst]
        amount = min(equity(w) * TRANSFER_FRACTION, w["cash"])
        if amount >= 0.01:
            w["cash"] -= amount
            state["agents"][best]["cash"] += amount
            standings = ", ".join(
                f"{n} £{equity(a):.2f}" for n, a in state["agents"].items()
            )
            alerts.append(
                f"Hourly rebalance: {worst} ({perf[worst] * 100:+.1f}% this hour) "
                f"gave £{amount:.2f} to {best} ({perf[best] * 100:+.1f}%).\n"
                f"Now: {standings}"
            )
    for ag in state["agents"].values():
        ag["snapshot"] = equity(ag)
    state["last_rebalance"] = now


def main():
    state = load_state()
    now = time.time()
    held = {a for ag in state["agents"].values() for a in ag["positions"]}
    cands = candidate_addresses()
    pairs = fetch_pairs(list(held | set(cands)))
    alerts = []

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
                ag["closed"].append({"symbol": pos["symbol"], "pnl": round(pnl, 4)})
                ag["closed"] = ag["closed"][-500:]
                del ag["positions"][addr]
                alerts.append(
                    f"[{name}] SELL {pos['symbol']} ({reason}) "
                    f"{ret * 100:+.1f}%, P&L £{pnl:+.2f}"
                )

        for addr in cands:
            if len(ag["positions"]) >= MAX_POSITIONS:
                break
            size = min(ag["cash"], POSITION_FRACTION * equity(ag))
            if size < 1.0:
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
            ag["cash"] -= size
            ag["positions"][addr] = {
                "symbol": sym,
                "entry_price": price,
                "last_price": price,
                "qty": size * (1 - FEE) / price,
                "cost": size,
                "opened_at": now,
            }
            alerts.append(f"[{name}] BUY {sym} £{size:.2f}")

    rebalance(state, now, alerts)

    utc = datetime.now(timezone.utc)
    today = utc.strftime("%Y-%m-%d")
    if state["last_report"] != today and utc.hour >= 7:
        lines = ["Paper leaderboard:"]
        ranked = sorted(state["agents"].items(), key=lambda kv: -equity(kv[1]))
        total = 0.0
        for name, ag in ranked:
            eq = equity(ag)
            total += eq
            c = ag["closed"]
            wins = sum(1 for t in c if t["pnl"] > 0)
            wr = f"{wins / len(c) * 100:.0f}%" if c else "n/a"
            lines.append(
                f"{name}: £{eq:.2f}, {len(c)} trades, "
                f"win rate {wr}, open {len(ag['positions'])}"
            )
        start_total = START_CASH * len(AGENTS)
        lines.append(
            f"Total: £{total:.2f} vs £{start_total:.2f} start "
            f"({(total / start_total - 1) * 100:+.1f}%)"
        )
        alerts.append("\n".join(lines))
        state["last_report"] = today

    for a in alerts:
        send(a)
    save_state(state)


main()
