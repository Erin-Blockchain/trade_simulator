from flask import Flask, render_template, request, jsonify
import math
import time
from concurrent.futures import ThreadPoolExecutor

# token-intelligence backend (Transfer-replay + swap read, validated vs GMGN)
import rh_pipeline as P
import rh_holder_features as HF
import rh_curve_swaps as CS
import threading
from concurrent.futures import ThreadPoolExecutor

# Tracks background job states: {job_id: {"status": "processing"|"completed"|"error", "result": {...}}}
JOB_STORE = {}
background_executor = ThreadPoolExecutor(max_workers=2)
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
    data = request.json or {}
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


def _avg_order_sizes(token, curve, lblock, target, lt, ts, qscale, qprice):
    """Return (avg_buy_usd, avg_sell_usd, n_buys, n_sells) using pre-fetched metadata."""
    class _Const(dict):
        def __init__(self, v): self._v = v
        def get(self, *_a, **_k): return self._v
        
    rows = CS.build_stitched_once(
        _rpc_solid, token, curve, lblock, target,
        block_times=_Const(ts), quote_price=lambda t: qprice,
        quote_scale=qscale, supply=1_000_000_000, log_chunk=10000,
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
    try:
        data = request.json or {}
        token = (data.get('token') or '').strip().lower()
        chain = (data.get('chain') or 'robinhood').lower()
        unit = (data.get('unit') or 'min').lower()      
        try:
            age = float(data.get('age', 15))
        except (TypeError, ValueError):
            age = 15.0

        age_min = age * 60 if unit in ('hour', 'hours', 'hr', 'h') else age
        job_key = f"{token}_{age_min}"

        # 1. Return completed data instantly if cached
        if job_key in JOB_STORE and JOB_STORE[job_key].get("status") == "completed":
            return jsonify(JOB_STORE[job_key]["result"])

        # 2. If a background job is actively running, tell the frontend to keep waiting
        if job_key in JOB_STORE and JOB_STORE[job_key].get("status") == "processing":
            return jsonify({"status": "processing", "message": "Historical replay in progress..."}), 202

        # 3. If a previous run failed, clear it and return the error
        if job_key in JOB_STORE and JOB_STORE[job_key].get("status") == "error":
            err_msg = JOB_STORE[job_key].get("message")
            del JOB_STORE[job_key]
            return jsonify({"error": err_msg}), 500

        if chain not in ('robinhood',):
            return jsonify({"error": f"Chain '{chain}' not supported yet."}), 400
        if not (token.startswith('0x') and len(token) == 42):
            return jsonify({"error": "Enter a valid token address (0x + 40 hex)."}), 400
        if age_min <= 0:
            return jsonify({"error": "Age must be greater than 0."}), 400

        # 4. Mark as processing and launch background thread
        JOB_STORE[job_key] = {"status": "processing"}

        def run_background_job():
            try:
                feats = {}
                avg_buy = avg_sell = 0.0
                n_buys = n_sells = 0

                lblock, curve, deployer = _find_launch(token)
                if lblock is None:
                    JOB_STORE[job_key] = {"status": "error", "message": "Launch event not found for this token."}
                    return

                tip = P.latest_block()
                target = min(lblock + int(age_min * 60 * BLOCKS_PER_SEC), tip)
                coin_age_now_min = (tip - lblock) / BLOCKS_PER_SEC / 60.0
                capped = (lblock + int(age_min * 60 * BLOCKS_PER_SEC)) > tip

                with ThreadPoolExecutor(max_workers=3) as executor:
                    future_feats = executor.submit(
                        HF.compute, _rpc_solid, token, curve, deployer,
                        P.PONS_FACTORIES[0][1], lblock, target, log_chunk=10000
                    )
                    future_lt = executor.submit(CS.launched_token, _rpc_solid, token)
                    lt = future_lt.result() or {}
                    pair = (lt.get("pairToken") or CS.NATIVE).lower()

                    future_ts_target = executor.submit(P.block_timestamp, target)
                    future_ts_lblock = executor.submit(P.block_timestamp, lblock)
                    ts = future_ts_target.result() or future_ts_lblock.result()

                    try:
                        eth = P.eth_usd(ts) if hasattr(P, "eth_usd") else 1920.0
                    except Exception:
                        eth = 1920.0

                    qscale, qprice, _, _ = executor.submit(CS.resolve_quote, _rpc_solid, pair, eth, ts=ts).result()

                    future_swaps = executor.submit(
                        _avg_order_sizes, token, curve, lblock, target, lt, ts, qscale, qprice
                    )

                    try:
                        avg_buy, avg_sell, n_buys, n_sells = future_swaps.result()
                    except Exception as e:
                        print(f"Error computing order sizes: {e}")

                    try:
                        feats = future_feats.result() or {}
                    except Exception as e:
                        print(f"RPC error during holder replay: {e}")

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
                    out["note"] = f"Coin is only {coin_age_now_min:.1f} min old; showing data at its current age."

                JOB_STORE[job_key] = {"status": "completed", "result": out}
            except Exception as e:
                import traceback
                traceback.print_exc()
                JOB_STORE[job_key] = {"status": "error", "message": str(e)}

        background_executor.submit(run_background_job)
        return jsonify({"status": "processing", "message": "Initial scan initiated..."}), 202

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {type(e).__name__}: {str(e)[:200]}"}), 500
def _intel_impl():
    data = request.json or {}
    token = (data.get('token') or '').strip().lower()
    chain = (data.get('chain') or 'robinhood').lower()
    unit = (data.get('unit') or 'min').lower()      
    try:
        age = float(data.get('age', 15))
    except (TypeError, ValueError):
        age = 15.0

    age_min = age * 60 if unit in ('hour', 'hours', 'hr', 'h') else age

    if chain not in ('robinhood',):
        return jsonify({"error": f"Chain '{chain}' not supported yet."}), 400
    if not (token.startswith('0x') and len(token) == 42):
        return jsonify({"error": "Enter a valid token address (0x + 40 hex)."}), 400
    if age_min <= 0:
        return jsonify({"error": "Age must be greater than 0."}), 400

    feats = {}
    avg_buy = avg_sell = 0.0
    n_buys = n_sells = 0

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as executor:
        # 1. Fetch block data, block tips, and token info simultaneously
        future_launch = executor.submit(_find_launch, token)
        future_tip = executor.submit(P.latest_block)
        future_lt = executor.submit(CS.launched_token, _rpc_solid, token)

        try:
            lblock, curve, deployer = future_launch.result()
        except Exception as e:
            return jsonify({"error": f"RPC error while locating launch: {str(e)[:120]}"}), 502

        if lblock is None:
            return jsonify({"error": "Launch event not found for this token (not a pons token, or too old)."}), 404

        tip = future_tip.result()
        target = min(lblock + int(age_min * 60 * BLOCKS_PER_SEC), tip)
        coin_age_now_min = (tip - lblock) / BLOCKS_PER_SEC / 60.0
        capped = (lblock + int(age_min * 60 * BLOCKS_PER_SEC)) > tip

        # 2. Kick off the heavy holder replay task immediately to overlap with dependent API calls
        future_feats = executor.submit(
            HF.compute, _rpc_solid, token, curve, deployer,
            P.PONS_FACTORIES[0][1], lblock, target, log_chunk=10000
        )

        # 3. Resolve interdependent swap metadata sequentially but concurrently to HF.compute
        # Resolve interdependent swap metadata concurrently
        lt = future_lt.result() or {}
        pair = (lt.get("pairToken") or CS.NATIVE).lower()

        # Fire both block timestamp requests at the same time
        future_ts_target = executor.submit(P.block_timestamp, target)
        future_ts_lblock = executor.submit(P.block_timestamp, lblock)
        ts = future_ts_target.result() or future_ts_lblock.result()

        try:
            eth = P.eth_usd(ts) if hasattr(P, "eth_usd") else 1920.0
        except Exception:
            eth = 1920.0

        qscale, qprice, _, _ = executor.submit(CS.resolve_quote, _rpc_solid, pair, eth, ts=ts).result()

        # 4. Perform the swap loops with pre-fetched metadata
        future_swaps = executor.submit(
            _avg_order_sizes, token, curve, lblock, target, lt, ts, qscale, qprice
        )

        try:
            avg_buy, avg_sell, n_buys, n_sells = future_swaps.result()
        except Exception as e:
            print(f"Error computing order sizes: {e}")

        try:
            feats = future_feats.result() or {}
        except Exception as e:
            print(f"RPC error during holder replay: {e}")

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
