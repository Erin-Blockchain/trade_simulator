from flask import Flask, render_template, request, jsonify
import math
import os
import sys
import threading
import time
import types
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

# token-intelligence backend: Robinhood (Transfer-replay + swap read, validated vs GMGN)
import rh_pipeline as P
import rh_holder_features as HF
import rh_curve_swaps as CS

# shared EVM helpers from the BSC pipeline (verified code, imported, not rebuilt):
#   token_transfers : Transfer logs WITH tx hash + log index (every block covered
#                     or it raises); also the whole BSC path below
import bsc_core as C
from bsc_rpc import Pool, Endpoint, load_endpoints, head as bsc_head, block_ts as bsc_block_ts

# The Robinhood collector's own code, for ETH/USD (rh_collect_V2.eth_usd, the
# price every RH row was built with) and the buyer attribution of
# unique_buyers_15 (rh_backfill_v2.buy_txs). rh_collect_V2 imports
# rh_mcap_cache, which only its batch collector writes with: an empty stand-in
# is used ONLY if that file is not on this server; nothing here calls it.
try:
    import rh_mcap_cache  # noqa: F401
except ImportError:
    sys.modules["rh_mcap_cache"] = types.ModuleType("rh_mcap_cache")
import rh_collect_V2 as B
import rh_backfill_v2 as V2

# Tracks background job states: {job_id: {"status": "processing"|"completed"|"error", "result": {...}}}
JOB_STORE = {}
background_executor = ThreadPoolExecutor(max_workers=2)
app = Flask(__name__)

LAUNCH_TOPIC = ("0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051f"
                "b6aa43a89607")
SUPPORTED_CHAINS = ("robinhood", "bsc")


def binomial_pmf(n, k, p):
    return math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))


# --------------------------------------------------------------------------- #
# Research articles: research/index.json lists them, research/<slug>.html holds
# each body (pre-rendered from markdown), static/research/<slug>.png the cover.
# --------------------------------------------------------------------------- #
import json  # noqa: E402
from flask import abort  # noqa: E402

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research")


def _research_index():
    try:
        with open(os.path.join(RESEARCH_DIR, "index.json"), encoding="utf-8") as f:
            return sorted(json.load(f), key=lambda a: a["ep"])
    except (OSError, ValueError):
        return []


@app.route('/')
def home():
    return render_template('index.html', articles=_research_index())


@app.route('/research/<slug>')
def research_article(slug):
    arts = _research_index()
    pos = next((i for i, a in enumerate(arts) if a["slug"] == slug), None)
    if pos is None:                      # only slugs from the index: no path tricks
        abort(404)
    with open(os.path.join(RESEARCH_DIR, slug + ".html"), encoding="utf-8") as f:
        body = f.read()
    return render_template('article.html', a=arts[pos], body=body,
                           prev=arts[pos - 1] if pos > 0 else None,
                           next=arts[pos + 1] if pos + 1 < len(arts) else None)


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
# Token intelligence: holder + trade metrics at ANY historical age of the coin.
# Inputs: chain + token address + age. The snapshot moment is launch time +
# age on REAL block timestamps (never a block count).
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


def _money(x):
    return round(float(x), 2)


def _order_sizes(rows):
    """(avg_buy_usd, avg_sell_usd, n_buys, n_sells): the app's existing formula."""
    buy_vol = sum(r["quote_usd"] for r in rows if r["side"] == "buy")
    sell_vol = sum(r["quote_usd"] for r in rows if r["side"] == "sell")
    n_buys = sum(1 for r in rows if r["side"] == "buy")
    n_sells = sum(1 for r in rows if r["side"] == "sell")
    return ((buy_vol / n_buys) if n_buys else 0.0,
            (sell_vol / n_sells) if n_sells else 0.0, n_buys, n_sells)


_FN = []


def _bsc_feat_new():
    """bsc_feat_new holds features_ab (largest buy/sell, top5 buyer share).
    It parses argv at import, so give it a dummy one (the same trick
    bsc_feat_new itself uses to import bsc_lp15_test). It also imports
    bsc_watch_verify, which only its CLI uses: an empty stand-in is put in
    place ONLY if that file is not on this server; nothing here calls it."""
    if not _FN:
        try:
            import bsc_watch_verify  # noqa: F401
        except ImportError:
            sys.modules["bsc_watch_verify"] = types.ModuleType("bsc_watch_verify")
        saved = sys.argv
        sys.argv = ["bsc_feat_new.py", "_seekal_"]
        try:
            import bsc_feat_new
        finally:
            sys.argv = saved
        _FN.append(bsc_feat_new)
    return _FN[0]


# =========================================================================== #
# ROBINHOOD
# =========================================================================== #

_LAUNCH_CACHE = {}      # token -> (lblock, curve, deployer); launches never change
_RH_POOL = []


def _rh_pool():
    """bsc_rpc Pool over the Robinhood endpoints, only for the Transfer read
    (bsc_core.token_transfers: every block covered or it raises)."""
    if not _RH_POOL:
        _RH_POOL.append(Pool([Endpoint(u) for u in P.RH_RPC_URLS]))
    return _RH_POOL[0]


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


def V3_per_tx(transfers, bmax):
    """rh_backfill_v3._per_tx: tx -> {address: net token change} up to bmax."""
    d = defaultdict(lambda: defaultdict(int))
    for blk, f, t, a, tx, _li in transfers:
        if blk <= bmax:
            d[tx][f] -= a
            d[tx][t] += a
    return d


class _Const(dict):
    def __init__(self, v): self._v = v
    def get(self, *_a, **_k): return self._v


def rh_intel(token, age_min):
    lblock, curve, deployer = _find_launch(token)
    if lblock is None:
        raise LookupError("Launch event not found for this token.")
    factory = P.PONS_FACTORIES[0][1].lower()

    tip = P.latest_block()
    tip_ts = CS._block_ts_req(_rpc_solid, tip)
    launch_ts = CS._block_ts_req(_rpc_solid, lblock)
    end_ts = int(launch_ts + age_min * 60)

    # snapshot = the LAST block stamped at or before launch + age (every block
    # of that second included), found on real timestamps
    capped = end_ts + 1 > tip_ts
    if capped:
        b_end, end_ts = tip, tip_ts
    else:
        b_end = CS.find_block_at_ts(_rpc_solid, end_ts + 1, lblock, launch_ts,
                                    tip, tip_ts) - 1
    coin_age_now_min = (tip_ts - launch_ts) / 60.0

    lt = CS.launched_token(_rpc_solid, token) or {}
    pair = (lt.get("pairToken") or CS.NATIVE).lower()
    # quote priced as the collector / backfills do: ETH/USD from
    # rh_collect_V2.eth_usd (raises if Binance has no price within 1 h, never a
    # made-up number), non-ETH pairs via resolve_quote with the same days
    qscale, qprice, _, _ = CS.resolve_quote(_rpc_solid, pair, B.eth_usd(end_ts),
                                            ts=end_ts, days=B.PAIR_SERIES_DAYS)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_rows = ex.submit(
            CS.build_stitched_once, _rpc_solid, token, curve, lblock, b_end,
            block_times=_Const(end_ts), quote_price=lambda t: qprice,
            quote_scale=qscale, supply=1_000_000_000, log_chunk=10000,
            pause=0.0, launched=lt)
        # Transfer logs once, with tx/log index; the 4-tuple view feeds HF
        f_trs = ex.submit(C.token_transfers, _rh_pool(), token, lblock, b_end)
        rows = f_rows.result()
        trs = f_trs.result()

    feats = HF.compute(_rpc_solid, token, curve, deployer, factory, lblock, b_end,
                       transfers=[(b, f, t, a) for b, f, t, a, _tx, _li in trs]) or {}
    avg_buy, avg_sell, n_buys, n_sells = _order_sizes(rows)

    liquidity = CS.liq_15(_rpc_solid, token, lt, pair, rows, lblock, b_end,
                          qprice, qscale, log_chunk=10000, pause=0.0) if rows else 0.0

    # Robinhood's own definitions (rh_backfill_v2 / rh_backfill_v3 / rh_feat_raw):
    #   system = dead/zero, token, curve, v4 PoolManager, v4 hook, factory
    #   venues = the curve, plus the PoolManager once the window has v4 trades
    pm, hook = CS.V4_POOL_MANAGER.lower(), CS.V4_MEME_HOOK.lower()
    system = V2.DEAD | {token.lower(), curve, pm, hook, factory}
    venues = {curve} | ({pm} if any(r["venue"] == "v4" for r in rows) else set())
    # unique_buyers: rh_backfill_v2 window() -> len(per_wallet) over V2.buy_txs
    unique_buyers = len({w for b, w, _tx in V2.buy_txs(trs, token.lower(), venues, system)
                         if b <= b_end})
    # repeat_buyer_pct: rh_backfill_v3 window(), the same lines (that module
    # can't be imported here: it sets CS.PER_TX = False for the whole process)
    blk_of = {tx: blk for blk, _f, _t, _a, tx, _li in trs}
    sb = []
    for tx, d in V3_per_tx(trs, b_end).items():
        if -sum(d.get(v, 0) for v in venues) <= 0:
            continue
        c = [(n, a) for a, n in d.items() if n > 0 and a not in system | venues]
        if c:
            sb.append((blk_of[tx], max(c)[1]))
    per = Counter(b for _, b in sb)
    repeat_pct = round(100 * sum(1 for c in per.values() if c >= 2) / len(per), 4) if per else 0.0
    # largest buy / sell, top 5 buyer share: rh_feat_raw.features reads its
    # rows from saved raw files, so the same formulas run through
    # bsc_feat_new.features_ab (its line-for-line port) with rh_feat_raw's
    # skip set: system | {curve, PoolManager}
    skip = system | {curve, pm}
    ab = _bsc_feat_new().features_ab(rows, trs, token.lower(), launch_ts, deployer, skip,
                                     full=max(1, math.ceil(age_min)), empty_ok=True)

    out = {
        "launch_block": lblock, "target_block": b_end,
        "coin_age_now_min": round(coin_age_now_min, 1),
        "holders": feats.get("holders", 0),
        "top10_pct": feats.get("top10_pct", 0.0),
        "dev_pct": feats.get("dev_pct", 0.0),
        "avg_buy_usd": _money(avg_buy), "avg_sell_usd": _money(avg_sell),
        "n_buys": n_buys, "n_sells": n_sells,
        "trades": len(rows),
        "unique_buyers": unique_buyers,
        "repeat_buyer_pct": repeat_pct,
        "liquidity_usd": _money(liquidity),
        "largest_buy_usd": ab["largest_buy_usd_15"],
        "largest_sell_usd": ab["largest_sell_usd_15"],
        "top5_buyer_share": ab["top5_buyer_share_15"],
    }
    if capped:
        out["note"] = f"Coin is only {coin_age_now_min:.1f} min old; showing data at its current age."
    return out


# =========================================================================== #
# BSC (Flap)
# =========================================================================== #
# Endpoints come from bsc_rpc_endpoints.txt (one URL per line). NOT
# rpc_endpoints.txt: rh_pipeline reads that file as its ROBINHOOD endpoints.
# The Portal cache (bsc_portal.sqlite) starts empty here and fills only with
# the block ranges users have searched, like the collector's cache.

BSC_RPC_FILE = os.environ.get("BSC_RPC_FILE", "bsc_rpc_endpoints.txt")
BSC_DB = os.environ.get("BSC_PORTAL_DB", "bsc_portal.sqlite")
# how far back the launch search walks from the chain head
BSC_LAUNCH_LOOKBACK_DAYS = float(os.environ.get("BSC_LAUNCH_LOOKBACK_DAYS", "75"))
BSC_LAUNCH_WINDOW = 200_000          # blocks per backward step (~25 h)

_BSC = {}
_BSC_LOCK = threading.Lock()


def _bsc():
    with _BSC_LOCK:
        if not _BSC:
            if not os.path.exists(BSC_RPC_FILE):
                raise RuntimeError(f"BSC not configured: {BSC_RPC_FILE} is missing on this server.")
            urls = [l.strip() for l in open(BSC_RPC_FILE, encoding="utf-8")
                    if l.strip() and not l.strip().startswith("#")]
            if not urls:
                raise RuntimeError(f"BSC not configured: {BSC_RPC_FILE} has no URLs.")
            _BSC["pool"] = Pool(load_endpoints(urls))
            _BSC["store"] = C.PortalStore(BSC_DB)
            _BSC["pricer"] = C.QuotePricer()
        return _BSC["pool"], _BSC["store"], _BSC["pricer"]


def _bsc_mint_block(pool, token, hb):
    """Block of the token's FIRST mint (Transfer from 0x0). Flap mints inside
    the launch tx, so this is the launch block. Walks back from the head one
    window at a time; after the first hit it keeps going until a window is
    empty, so the earliest mint is returned, not just the latest one."""
    from bsc_rpc import scan_logs
    zero_topic = "0x" + "0" * 64
    floor = max(1, hb - int(BSC_LAUNCH_LOOKBACK_DAYS * 86400 / 0.45))
    hi, first = hb, None
    while hi >= floor:
        lo = max(floor, hi - BSC_LAUNCH_WINDOW + 1)
        logs = scan_logs(pool, token, [C.TRANSFER, zero_topic], lo, hi, chunk=50_000,
                         concurrency=4, label=f"mint {token[:8]}")
        if logs:
            first = min(C.h2i(l["blockNumber"]) for l in logs)
        elif first is not None:
            break
        hi = lo - 1
    return first


def _fill_portal(pool, store, b0, b1):
    """Make every Portal event in [b0, b1] available in the store.
    Complete 2,000-block segments go through store.ensure as always (cached
    and marked done). The newest, still-open segment can't be marked done, so
    its blocks up to b1 are read straight from the chain and loaded with
    store._ingest(logs, None): the store's own no-segment path (the live
    watcher's). When that segment completes, a later ensure reads it whole;
    rows are keyed (tx, log index), so nothing is counted twice."""
    from bsc_rpc import scan_logs
    last_ok = ((store.safe_head + 1) // C.SEG) * C.SEG - 1   # last block of the last complete segment
    if b0 <= last_ok:
        store.ensure(pool, b0, min(b1, last_ok))
    t0 = max(b0, last_ok + 1)
    if t0 <= b1:
        logs = scan_logs(pool, C.PORTAL, C.STORE_TOPICS, t0, b1, chunk=C.SEG, concurrency=4,
                         label=f"portal tail {t0}")
        store._ingest(logs, None)


class _Filled:
    """The store, for C.stitched, after _fill_portal already loaded the range:
    its own ensure() would refuse the still-open segment (NotReady)."""
    def __init__(self, store):
        self._s = store

    def ensure(self, *_a, **_k):
        return 0

    def __getattr__(self, k):
        return getattr(self._s, k)


class _AgeClock(C.Clock):
    """C.Clock cut at the snapshot: any moment at or after launch + age maps to
    the snapshot block. bsc_core's feature functions also look at +5/+10/+15
    min; for a younger snapshot those are later than it, so they just see the
    snapshot (none of the numbers shown come from those windows). Before the
    snapshot it is C.Clock unchanged, except near the chain head, where the
    store may hold no trade after the moment yet: then the search is bounded
    by the safe head (real block timestamps, as last_block_le does)."""
    def __init__(self, pool, lb, lu, store, safe_block, safe_ts):
        super().__init__(pool, lb, lu, store)
        self.safe_block, self.safe_ts = safe_block, safe_ts
        self.end_ts = self.b_end = None

    def last_block(self, ts):
        if self.end_ts is not None and ts >= self.end_ts:
            return self.b_end
        if ts >= self.safe_ts:
            return self.safe_block
        if ts not in self.last and self.store.ts_bracket(ts)[1] is None:
            lo, hi = self.lb, self.safe_block           # ts(lo) <= ts < ts(hi)
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if bsc_block_ts(self.pool, mid) <= ts:
                    lo = mid
                else:
                    hi = mid
            self.last[ts] = lo
        return super().last_block(ts)


def _bsc_launch(pool, store, token):
    L = store.launch(token)
    if L:
        return L
    hb = bsc_head(pool)
    store.safe_head = hb - 20
    mb = _bsc_mint_block(pool, token, store.safe_head)
    if mb is None:
        raise LookupError("No mint found for this token on BSC "
                          f"(searched the last {BSC_LAUNCH_LOOKBACK_DAYS:g} days). "
                          "If it launched seconds ago, try again shortly.")
    _fill_portal(pool, store, mb, mb)       # the launch block's Portal events
    L = store.launch(token)
    if not L:
        raise LookupError("This token is not a Flap launch (no TokenCreated in its mint block).")
    return L


def bsc_intel(token, age_min):
    """The collector's steps (bsc_core._stage_30) with the end moment set to
    launch + age instead of +30:00; no gate. Any age, up to the coin's current
    age (like Robinhood). Every number comes from the collector's own functions."""
    FN = _bsc_feat_new()
    pool, store, pricer = _bsc()
    tok = token.lower()
    L = _bsc_launch(pool, store, tok)
    if L["migrator"] == 3:
        raise LookupError("Graduated to a PancakeSwap Infinity CL pool: not supported.")
    lu, lb = int(L["ts"]), int(L["block"])

    hb = bsc_head(pool)
    store.safe_head = safe = hb - 20          # ~9 s behind the head: settled blocks only
    safe_ts = bsc_block_ts(pool, safe)
    coin_age_now_min = (safe_ts - lu) / 60.0

    end_ts = int(lu + age_min * 60)
    capped = end_ts >= safe_ts
    if capped:
        end_ts = safe_ts
    clock = _AgeClock(pool, lb, lu, store, safe, safe_ts)
    if capped:
        b_end = safe
        _fill_portal(pool, store, lb, b_end)
    else:
        last_ok = ((safe + 1) // C.SEG) * C.SEG - 1
        if last_ok >= lb and end_ts < bsc_block_ts(pool, last_ok):
            b_end = clock.last_block(end_ts)      # every block stamped <= launch + age
            _fill_portal(pool, store, lb, b_end)
        else:                                     # snapshot inside the open segment
            _fill_portal(pool, store, lb, safe)
            b_end = clock.last_block(end_ts)
    clock.end_ts, clock.b_end = end_ts, b_end
    F = age_min if not capped else (end_ts - lu) / 60.0
    F = int(F) if float(F).is_integer() else F

    q, qsrc, qage = pricer.usd(L["quote"], end_ts, rpc=pool, block_fn=lambda: b_end, closed=True)
    qdec = C.erc20_decimals(pool, L["quote"])
    trs = C.token_transfers(pool, tok, lb, b_end)
    rows, grad, _exc, _dir = C.stitched(pool, _Filled(store), L, lb, b_end, qdec, lambda: trs)

    w = []
    for r in C._window(rows, end_ts, clock):
        r = dict(r)
        r["price_usd"] = r["price_q"] * q
        r["quote_usd"] = r["quote"] * q
        r["usd_per_quote"] = q
        w.append(r)

    dev = C.launch_sender(pool, L)
    g = grad if (grad and grad["block"] <= b_end) else None
    venues = {C.PORTAL} | ({g["pool"].lower()} if g else set())
    f15 = C.features_15(w, L, lu, trs, dev, venues, clock, full=F, empty_ok=True)
    v2, _early = C.features_v2(w, L, lu, trs, dev, venues, clock, q,
                               g["block"] if g else None,
                               bsc_block_ts(pool, g["block"]) if g else None, full=F)
    liquidity = round(2 * w[-1]["reserve_q"] * q, 2) if w else 0.0

    # features_ab needs a time on every row: PancakeSwap V2 rows carry only a block
    wt = [dict(r) for r in w]
    FN._times(pool, wt)
    wt.sort(key=lambda r: (r["t"], r["block"], r.get("li", 0)))
    skip = ({C.ZERO, C.DEAD, tok, C.PORTAL, C.VAULT_PORTAL, C.TAX_TOKEN_HELPER} | venues
            | C.tax_holders(trs, tok))
    ab = FN.features_ab(wt, trs, tok, lu, dev, skip, full=max(1, math.ceil(F)), empty_ok=True)
    avg_buy, avg_sell, n_buys, n_sells = _order_sizes(w)

    out = {
        "launch_block": lb, "target_block": b_end,
        "coin_age_now_min": round(coin_age_now_min, 1),
        "holders": f15["holders"], "top10_pct": f15["top10_pct"], "dev_pct": f15["dev_pct"],
        "avg_buy_usd": _money(avg_buy), "avg_sell_usd": _money(avg_sell),
        "n_buys": n_buys, "n_sells": n_sells,
        "trades": f15[f"trades_{F}"],
        "unique_buyers": v2[f"unique_buyers_{F}"],
        "repeat_buyer_pct": f15["repeat_buyer_pct"],
        "liquidity_usd": liquidity,
        "largest_buy_usd": ab["largest_buy_usd_15"],
        "largest_sell_usd": ab["largest_sell_usd_15"],
        "top5_buyer_share": ab["top5_buyer_share_15"],
        "quote_src": qsrc,
    }
    if capped:
        out["note"] = f"Coin is only {coin_age_now_min:.1f} min old; showing data at its current age."
    return out


# =========================================================================== #
# What a visitor sees when a query fails. Details stay in the server log.
GENERIC_ERROR = "Couldn't load this coin right now. Please try again in a minute."
SKIP_MESSAGES = {
    "unpriceable_quote": "This coin's trading pair has no USD price, so it can't be shown.",
    "unsupported_venue": "This coin trades on a pool type Seekal doesn't support yet.",
    "too_many_transfers": "This coin has too many token transfers to replay.",
}


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
        job_key = f"{chain}_{token}_{age_min}"      # chain in the key: same address, other chain

        # 1. Return completed data instantly if cached
        if job_key in JOB_STORE and JOB_STORE[job_key].get("status") == "completed":
            res = JOB_STORE[job_key]["result"]
            if res.get("note"):          # "showing its current age": recompute next time
                del JOB_STORE[job_key]
            return jsonify(res)

        # 2. If a background job is actively running, tell the frontend to keep waiting
        if job_key in JOB_STORE and JOB_STORE[job_key].get("status") == "processing":
            return jsonify({"status": "processing",
                            "progress": JOB_STORE[job_key].get("progress", 0),
                            "message": "Historical replay in progress..."}), 202

        # 3. If a previous run failed, clear it and return the error
        if job_key in JOB_STORE and JOB_STORE[job_key].get("status") == "error":
            err_msg = JOB_STORE[job_key].get("message")
            del JOB_STORE[job_key]
            return jsonify({"error": err_msg}), 500

        if chain not in SUPPORTED_CHAINS:
            return jsonify({"error": f"Chain '{chain}' not supported yet."}), 400
        if not (token.startswith('0x') and len(token) == 42):
            return jsonify({"error": "Enter a valid token address (0x + 40 hex)."}), 400
        if age_min <= 0:
            return jsonify({"error": "Age must be greater than 0."}), 400

        # 4. Mark as processing and launch background thread
        JOB_STORE[job_key] = {"status": "processing", "progress": 0}

        def run_background_job():
            try:
                fn = bsc_intel if chain == "bsc" else rh_intel
                out = {"token": token, "chain": chain, "unit": unit,
                       "age": age, "age_min": age_min, **fn(token, age_min)}
                JOB_STORE[job_key] = {"status": "completed", "result": out}
            except LookupError as e:          # our own plain-language messages
                JOB_STORE[job_key] = {"status": "error", "message": str(e)}
            except C.Skip as e:
                print(f"[intel] {chain} {token} skipped: {e}", flush=True)
                JOB_STORE[job_key] = {"status": "error",
                                      "message": SKIP_MESSAGES.get(e.outcome, GENERIC_ERROR)}
            except Exception:
                # the real error goes to the server log (journalctl -u tradeapp),
                # never to the visitor
                import traceback
                print(f"[intel] {chain} {token} age {age_min} min failed:", flush=True)
                traceback.print_exc()
                JOB_STORE[job_key] = {"status": "error", "message": GENERIC_ERROR}

        background_executor.submit(run_background_job)
        return jsonify({"status": "processing", "message": "Initial scan initiated..."}), 202

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": GENERIC_ERROR}), 500


if __name__ == '__main__':
    app.run(debug=True)
