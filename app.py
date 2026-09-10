from flask import Flask, render_template, request, jsonify
import math
import time

# token-intelligence backend (Transfer-replay + swap read, validated vs GMGN)
import rh_pipeline as P
import rh_holder_features as HF
import rh_curve_swaps as CS

app = Flask(__name__)

BLOCKS_PER_SEC = 10
LAUNCH_TOPIC = ("0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051f"
                "b6aa43a89607")


def binomial_pmf(n, k, p):
    return math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/api/simulate', methods=['POST'])
def simulate():
    data = request.json
    capital = float(data.get('capital', 100))
    sl_pct = float(data.get('sl_pct', 70))
    tp_mult = float(data.get('tp_mult', 10))
    win_rate_pct = float(data.get('win_rate', 66.66))
    n_trades = int(data.get('n_trades', 10))

    p = win_rate_pct / 100.0
    loss_amount = -1.0 * capital * (sl_pct / 100.0)
    profit_amount = (capital * tp_mult) - capital

    ev_per_trade = (p * profit_amount) + ((1.0 - p) * loss_amount)

    outcomes = []
    for wins in range(n_trades + 1):
        losses = n_trades - wins
        prob = binomial_pmf(n_trades, wins, p)
        total_pnl = (wins * profit_amount) + (losses * loss_amount)
        outcomes.append({"wins": wins, "losses": losses,
                         "prob": prob, "pnl": total_pnl})

    return jsonify({
        "ev_per_trade": ev_per_trade,
        "total_ev": ev_per_trade * n_trades,
        "outcomes": outcomes
    })


# --------------------------------------------------------------------------- #
# Token intelligence: holder metrics at ANY historical age of the coin.
# Inputs: token address + age (minutes since launch).
# --------------------------------------------------------------------------- #

def _rpc_solid(payload, tries=6):
    """RPC with retry; short timeout so the web request can't hang."""
    last = None
    for i in range(tries):
        try:
            j = P.post_rpc(payload, timeout=10)
            if isinstance(j, dict) and j.get("error"):
                raise RuntimeError(j["error"])
            return j
        except Exception as e:                              # noqa: BLE001
            last = e
            if i == tries - 1:
                raise
            time.sleep(1.0)
    raise last


_LAUNCH_CACHE = {}      # token -> (lblock, curve, deployer); launches never change


def _find_launch(token):
    """Return (lblock, curve, deployer) from the TokenLaunched event, cached
    (a launch never changes). Returns (None, None, None) if not found."""
    tok = token.lower()
    if tok in _LAUNCH_CACHE:
        return _LAUNCH_CACHE[tok]
    tt = "0x" + "0" * 24 + tok[2:]
    tip = P.latest_block()
    lo = tip
    while lo > tip - 3_000_000:
        frm = max(lo - 300_000, tip - 3_000_000)
        j = _rpc_solid({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                        "params": [{"address": P.PONS_FACTORIES[0][1],
                                    "topics": [LAUNCH_TOPIC, tt],
                                    "fromBlock": hex(frm),
                                    "toBlock": hex(lo)}]})
        res = (j or {}).get("result") or []
        if res:
            lg = res[0]
            lblock = int(lg["blockNumber"], 16)
            curve = ("0x" + lg["topics"][2][-40:]).lower()
            deployer = ("0x" + lg["topics"][3][-40:]).lower()
            _LAUNCH_CACHE[tok] = (lblock, curve, deployer)
            return lblock, curve, deployer
        lo = frm - 1
    return None, None, None


def _avg_order_sizes(token, curve, lblock, target):
    """Return (avg_buy_usd, avg_sell_usd, n_buys, n_sells) over
    [lblock, target] from the curve/v4 swap rows. USD per side."""
    lt = CS.launched_token(_rpc_solid, token) or {}
    pair = (lt.get("pairToken") or CS.NATIVE).lower()
    ts = P.block_timestamp(target) or P.block_timestamp(lblock)
    # eth price at the window (for native pairs); resolve_quote uses it
    try:
        eth = P.eth_usd(ts) if hasattr(P, "eth_usd") else 1920.0
    except Exception:
        eth = 1920.0
    qscale, qprice, _, _ = CS.resolve_quote(_rpc_solid, pair, eth, ts=ts)

    class _Const(dict):
        def __init__(self, v): self._v = v
        def get(self, *_a, **_k): return self._v
    rows = CS.build_stitched_once(
        _rpc_solid, token, curve, lblock, target,
        block_times=_Const(ts), quote_price=lambda t: qprice,
        quote_scale=qscale, supply=1_000_000_000, log_chunk=2500,
        pause=0.0, launched=lt)

    buy_vol = sum(r["quote_usd"] for r in rows if r["side"] == "buy")
    sell_vol = sum(r["quote_usd"] for r in rows if r["side"] == "sell")
    n_buys = sum(1 for r in rows if r["side"] == "buy")
    n_sells = sum(1 for r in rows if r["side"] == "sell")
    avg_buy = (buy_vol / n_buys) if n_buys else 0.0
    avg_sell = (sell_vol / n_sells) if n_sells else 0.0
    return avg_buy, avg_sell, n_buys, n_sells


@app.route('/api/intel', methods=['POST'])
def intel():
    data = request.json or {}
    token = (data.get('token') or '').strip().lower()
    chain = (data.get('chain') or 'robinhood').lower()
    unit = (data.get('unit') or 'min').lower()      # 'min' (default) or 'hour'
    try:
        age = float(data.get('age', 15))
    except (TypeError, ValueError):
        age = 15.0

    # normalise age to minutes
    age_min = age * 60 if unit in ('hour', 'hours', 'hr', 'h') else age

    if chain not in ('robinhood',):
        return jsonify({"error": f"Chain '{chain}' not supported yet."}), 400
    if not (token.startswith('0x') and len(token) == 42):
        return jsonify({"error": "Enter a valid token address (0x + 40 hex)."}), 400
    if age_min <= 0:
        return jsonify({"error": "Age must be greater than 0."}), 400

    try:
        lblock, curve, deployer = _find_launch(token)
    except Exception as e:                                  # noqa: BLE001
        return jsonify({"error": f"RPC error while locating launch: "
                                 f"{str(e)[:120]}"}), 502
    if lblock is None:
        return jsonify({"error": "Launch event not found for this token "
                                 "(not a pons token, or too old)."}), 404

    tip = P.latest_block()
    target = min(lblock + int(age_min * 60 * BLOCKS_PER_SEC), tip)
    coin_age_now_min = (tip - lblock) / BLOCKS_PER_SEC / 60.0
    capped = (lblock + int(age_min * 60 * BLOCKS_PER_SEC)) > tip

    # holder features (holders, top10, dev) — drop burnt
    try:
        feats = HF.compute(_rpc_solid, token, curve, deployer,
                           P.PONS_FACTORIES[0][1], lblock, target) or {}
    except Exception as e:                                  # noqa: BLE001
        return jsonify({"error": f"RPC error during holder replay: "
                                 f"{str(e)[:120]}"}), 502

    # avg buy / sell order size from the swap rows
    try:
        avg_buy, avg_sell, n_buys, n_sells = _avg_order_sizes(
            token, curve, lblock, target)
    except Exception as e:                                  # noqa: BLE001
        avg_buy = avg_sell = 0.0
        n_buys = n_sells = 0

    out = {
        "token": token,
        "unit": unit,
        "age": age,
        "age_min": age_min,
        "launch_block": lblock,
        "target_block": target,
        "coin_age_now_min": round(coin_age_now_min, 1),
        "holders": feats.get("holders", 0),
        "top10_pct": feats.get("top10_pct", 0.0),
        "dev_pct": feats.get("dev_pct", 0.0),
        "avg_buy_usd": round(avg_buy, 2),
        "avg_sell_usd": round(avg_sell, 2),
        "n_buys": n_buys,
        "n_sells": n_sells,
    }
    if capped:
        out["note"] = (f"Coin is only {coin_age_now_min:.1f} min old; showing "
                       f"data at its current age.")
    return jsonify(out)


if __name__ == '__main__':
    app.run(debug=True)
