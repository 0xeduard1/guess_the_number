#!/usr/bin/env python3
"""
One-off holder-risk scan for a Solana SPL token.

Independent implementation of the ideas behind a holder scanner (concentration,
linked-wallet "operators", sell impact into a constant-product pool). Not a port
of any third-party code.

usage:
    python risk_scan.py <MINT> [--rpc URL] [--fast] [--json]

Only public JSON-RPC methods are used:
    getTokenSupply, getTokenLargestAccounts, getMultipleAccounts,
    getSignaturesForAddress, getTransaction
Nothing is signed or sent. Not financial advice.
"""
import argparse
import base64
import json
import struct
import sys
import time

import requests

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111"}
# Programs that hold tokens on behalf of a pool / curve / lock: never a "holder".
KNOWN_PROGRAMS = {
    PUMP_PROGRAM: "pump.fun bonding curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap pool",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEEm1VG": "Meteora DAMM v2",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora pools",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m": "Streamflow lock",
    "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn": "Jupiter lock",
}
KNOWN_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium AMM v4 authority",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM authority",
}

# ---- thresholds (tunable) ---------------------------------------------------
HISTORY_LIMIT = 100          # a wallet with fewer signatures than this is fully read
FRESH_MAX_TXS = 10           # <= this many txs in total → "fresh" wallet
MIN_HOLDERS = 10             # fewer real holders → too early to score

# component: (weight, value where it is still full marks, value where it hits zero)
COMPONENTS = {
    "sell_impact":   (40, 0.10, 0.60),  # price drop if the biggest operator dumps
    "fresh_wallets": (25, 0.10, 0.80),  # fraction of top wallets that are fresh
    "concentration": (20, 0.25, 0.70),  # top-N share of float
    "top_holder":    (15, 0.05, 0.30),  # single biggest wallet share of float
}


# ---- RPC --------------------------------------------------------------------
class Rpc:
    def __init__(self, url, min_interval=0.25):
        self.url, self.min_interval, self._last, self._id = url, min_interval, 0.0, 0
        self.s = requests.Session()

    def call(self, method, params):
        for attempt in range(6):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._id += 1
            self._last = time.time()
            try:
                r = self.s.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, timeout=30)
            except requests.RequestException:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"{method}: {body['error']}")
            return body["result"]
        raise RuntimeError(f"{method}: RPC kept failing (rate limit?) – try --rpc with your own endpoint")

    def multiple_accounts(self, keys, encoding="jsonParsed"):
        out = []
        for i in range(0, len(keys), 100):
            out += self.call("getMultipleAccounts", [keys[i:i + 100], {"encoding": encoding}])["value"]
        return out


# ---- math -------------------------------------------------------------------
def sell_impact(tokens_sold, reserve_tokens):
    """Price drop when `tokens_sold` go into an x*y=k pool holding `reserve_tokens`.
    Spot price ~ y/x; after selling q tokens x' = x+q, y' = k/x', so p'/p = (x/(x+q))^2."""
    if tokens_sold <= 0 or not reserve_tokens or reserve_tokens <= 0:
        return 0.0
    return 1 - (reserve_tokens / (reserve_tokens + tokens_sold)) ** 2


def linear_points(name, x):
    weight, full, zero = COMPONENTS[name]
    if x <= full:
        q = 1.0
    elif x >= zero:
        q = 0.0
    else:
        q = (zero - x) / (zero - full)
    return round(weight * q, 1)


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, i):
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


# ---- chain reading ----------------------------------------------------------
def read_curve_virtual_tokens(rpc, curve_address):
    """pump.fun bonding curve account: 8-byte discriminator, then virtual_token_reserves (u64, 6 decimals)."""
    acc = rpc.call("getAccountInfo", [curve_address, {"encoding": "base64"}])["value"]
    if not acc:
        return None
    raw = base64.b64decode(acc["data"][0])
    if len(raw) < 16:
        return None
    return struct.unpack_from("<Q", raw, 8)[0] / 1e6


def fetch_holders(rpc, mint):
    supply = rpc.call("getTokenSupply", [mint])["value"]
    total = float(supply["uiAmountString"])
    largest = rpc.call("getTokenLargestAccounts", [mint])["value"]
    token_accounts = [a["address"] for a in largest]
    parsed = rpc.multiple_accounts(token_accounts)

    rows = []
    for la, acc in zip(largest, parsed):
        amount = float(la["uiAmountString"] or 0)
        if amount <= 0 or not acc:
            continue
        owner = acc["data"]["parsed"]["info"]["owner"]
        rows.append({"token_account": la["address"], "owner": owner, "amount": amount})

    # Who controls each owner? A normal wallet is a System-owned (or empty) account.
    owners = sorted({r["owner"] for r in rows})
    owner_info = dict(zip(owners, rpc.multiple_accounts(owners, encoding="base64")))

    holders, infra = {}, []
    for r in rows:
        o = r["owner"]
        info = owner_info.get(o)
        program = info["owner"] if info else None
        label = None
        if o in BURN_ADDRESSES:
            label = "burn"
        elif o in KNOWN_AUTHORITIES:
            label = KNOWN_AUTHORITIES[o]
        elif program in KNOWN_PROGRAMS:
            label = KNOWN_PROGRAMS[program]
        elif program and program != SYSTEM_PROGRAM:
            label = f"program-owned ({program[:6]}…)"
        if label:
            infra.append({**r, "label": label, "program": program})
        else:
            h = holders.setdefault(o, {"owner": o, "amount": 0.0, "accounts": 0})
            h["amount"] += r["amount"]
            h["accounts"] += 1
    return total, sorted(holders.values(), key=lambda h: -h["amount"]), infra


def wallet_profile(rpc, wallet):
    """Fresh-wallet check + who funded it (fee payer / SOL sender of its first tx)."""
    sigs = rpc.call("getSignaturesForAddress", [wallet, {"limit": HISTORY_LIMIT}])
    n = len(sigs)
    prof = {"tx_count": n if n < HISTORY_LIMIT else f"{HISTORY_LIMIT}+", "fresh": n <= FRESH_MAX_TXS, "funder": None}
    if not sigs or n >= HISTORY_LIMIT:
        return prof  # long history: not fresh, first tx not reachable cheaply
    first = sigs[-1]["signature"]
    tx = rpc.call("getTransaction", [first, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    if not tx:
        return prof
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    pre, post = tx["meta"]["preBalances"], tx["meta"]["postBalances"]
    # funder = account whose SOL dropped the most in the wallet's first tx (other than the wallet itself)
    best, drop = None, 0
    for i, k in enumerate(keys):
        d = pre[i] - post[i]
        if k != wallet and d > drop:
            best, drop = k, d
    if best is None and keys and keys[0] != wallet:
        best = keys[0]
    prof["funder"] = best
    return prof


# ---- analysis ---------------------------------------------------------------
def analyse(mint, rpc, deep=True, log=print):
    log(f"reading supply and top holders of {mint} …")
    total, holders, infra = fetch_holders(rpc, mint)
    infra_amount = sum(i["amount"] for i in infra)
    float_tokens = max(total - infra_amount, 1e-9)

    # reserve = biggest pool/curve position among the top accounts
    pools = [i for i in infra if i["label"] not in ("burn",) and "lock" not in i["label"]]
    reserve, reserve_src = None, None
    if pools:
        p = max(pools, key=lambda i: i["amount"])
        reserve, reserve_src = p["amount"], p["label"]
        if p["program"] == PUMP_PROGRAM:
            try:
                v = read_curve_virtual_tokens(rpc, p["owner"])
                if v:
                    reserve, reserve_src = v, "pump.fun curve (virtual reserves)"
            except Exception:
                pass

    for h in holders:
        h["share"] = h["amount"] / float_tokens

    result = {"mint": mint, "total_supply": total, "float_tokens": float_tokens,
              "excluded": [{"owner": i["owner"], "label": i["label"], "amount": i["amount"]} for i in infra],
              "reserve_tokens": reserve, "reserve_source": reserve_src, "holders": holders}

    if len(holders) < MIN_HOLDERS:
        result.update(verdict="TOO EARLY", score=None,
                      note=f"only {len(holders)} real holders among the largest accounts")
        return result

    # --- operator detection: shared funder → linked wallets ------------------
    uf = UnionFind(len(holders))
    if deep:
        log(f"profiling {len(holders)} wallets (first tx + history size) …")
        for h in holders:
            try:
                h.update(wallet_profile(rpc, h["owner"]))
            except Exception as e:
                h.update(tx_count=None, fresh=False, funder=None, error=str(e))
        by_funder = {}
        for i, h in enumerate(holders):
            f = h.get("funder")
            if f:
                by_funder.setdefault(f, []).append(i)
        for f, idx in by_funder.items():
            for j in idx[1:]:
                uf.union(idx[0], j)
        # a funder that is itself a top holder links to its children too
        owner_idx = {h["owner"]: i for i, h in enumerate(holders)}
        for f, idx in by_funder.items():
            if f in owner_idx:
                uf.union(owner_idx[f], idx[0])

    groups = {}
    for i in range(len(holders)):
        groups.setdefault(uf.find(i), []).append(i)
    operators = sorted(
        ({"wallets": [holders[i]["owner"] for i in g], "share": sum(holders[i]["share"] for i in g),
          "funder": holders[g[0]].get("funder") if len(g) > 1 else None} for g in groups.values()),
        key=lambda o: -o["share"])
    n_op = 0
    for o in operators:
        if len(o["wallets"]) > 1:
            n_op += 1
            o["id"] = f"OP-{n_op}"
            for w in o["wallets"]:
                next(h for h in holders if h["owner"] == w)["operator"] = o["id"]
        else:
            o["id"] = None
    result["operators"] = operators

    # --- metrics ------------------------------------------------------------
    big = operators[0]
    m = {
        "concentration": sum(h["share"] for h in holders),
        "top_holder": holders[0]["share"],
        "fresh_wallets": (sum(1 for h in holders if h.get("fresh")) / len(holders)) if deep else 0.0,
        "sell_impact": sell_impact(big["share"] * float_tokens, reserve) if reserve else 0.0,
    }
    parts = {k: linear_points(k, m[k]) for k in COMPONENTS}
    if not deep:
        parts["fresh_wallets"] = None  # not measured
    measured_weight = sum(COMPONENTS[k][0] for k, v in parts.items() if v is not None)
    score = round(100 * sum(v for v in parts.values() if v is not None) / measured_weight)

    flags, caps = [], []
    if len(big["wallets"]) > 1 and m["sell_impact"] >= 0.5:
        flags.append(f"{big['id']} ({len(big['wallets'])} linked wallets, {big['share']:.1%} of float) could move price "
                     f"−{m['sell_impact']:.0%} if sold")
    if deep and m["fresh_wallets"] >= 0.8:
        flags.append(f"{m['fresh_wallets']:.0%} of top wallets are fresh (≤{FRESH_MAX_TXS} txs)")
    if len(big["wallets"]) == 1 and m["sell_impact"] >= 0.5:
        caps.append(f"one wallet could move price −{m['sell_impact']:.0%} (thin liquidity)")
    for o in operators:
        if o["id"] and len(o["wallets"]) >= 3:
            caps.append(f"{o['id']}: {len(o['wallets'])} wallets funded by the same address hold {o['share']:.1%} of float")
    if reserve is None:
        caps.append("no pool/curve found among the largest accounts · sell impact not measured")
    if caps:
        score = min(score, 59)
    band = "DANGER" if flags else "CLEAN" if score >= 80 else "OK" if score >= 60 else "RISKY" if score >= 40 else "DANGER"

    # --- sell scenario: linked + fresh wallets all sell -----------------------
    sus = [h for h in holders if h.get("operator") or h.get("fresh")]
    sus_share = sum(h["share"] for h in sus)
    scenario = None
    if reserve and sus:
        scenario = {"wallets": len(sus), "share_of_float": sus_share,
                    "price_drop": sell_impact(sus_share * float_tokens, reserve)}

    result.update(metrics=m, parts=parts, score=score, verdict=band, red_flags=flags, caps=caps, scenario=scenario)
    return result


# ---- output -----------------------------------------------------------------
def print_report(r):
    short = lambda a: a[:4] + "…" + a[-4:]
    print()
    print(f"mint        {r['mint']}")
    print(f"supply      {r['total_supply']:,.0f}   float {r['float_tokens']:,.0f}")
    for e in r["excluded"]:
        print(f"  excluded  {short(e['owner'])}  {e['label']:<32} {e['amount']:,.0f}")
    if r["reserve_tokens"]:
        print(f"reserve     {r['reserve_tokens']:,.0f} tokens · {r['reserve_source']}")
    if r["score"] is None:
        print(f"\n{r['verdict']} · {r.get('note', '')}")
        return
    print(f"\nSCORE {r['score']} / 100 · {r['verdict']}")
    labels = {"sell_impact": "Biggest operator dump", "fresh_wallets": "Fresh wallets",
              "concentration": "Top-holder share", "top_holder": "Largest single wallet"}
    for k, (w, _, _) in COMPONENTS.items():
        pts = r["parts"][k]
        print(f"  {labels[k]:<24} {r['metrics'][k]:>7.1%}   {'n/a' if pts is None else f'{pts:4.1f}'} / {w}")
    for f in r["red_flags"]:
        print("RED FLAG · " + f)
    for c in r["caps"]:
        print("CAP      · " + c)
    if r["scenario"] and r["scenario"]["price_drop"] > 0:
        s = r["scenario"]
        print(f"SCENARIO · if the {s['wallets']} linked/fresh wallets sold their {s['share_of_float']:.1%} of float, "
              f"price would fall ~{s['price_drop']:.0%}")
    print("\ntop holders")
    for h in r["holders"]:
        tags = [t for t in (h.get("operator"), "fresh" if h.get("fresh") else None) if t]
        tx = h.get("tx_count", "")
        print(f"  {short(h['owner'])}  {h['share']:6.2%}  txs={tx!s:<5} {' '.join(tags)}")
    print("\nHeuristic analysis, not financial advice.")


def main():
    ap = argparse.ArgumentParser(description="One-off holder-risk scan for a Solana token")
    ap.add_argument("mint")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help=f"JSON-RPC endpoint (default {DEFAULT_RPC})")
    ap.add_argument("--fast", action="store_true", help="skip per-wallet history (no fresh/operator detection)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args()
    rpc = Rpc(a.rpc)
    log = (lambda *x: print(*x, file=sys.stderr))
    r = analyse(a.mint, rpc, deep=not a.fast, log=log)
    if a.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
