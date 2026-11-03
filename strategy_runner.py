#!/usr/bin/env python3
"""
Kite Index Options Intraday Strategy Runner
Single-shot execution — reads market, decides, executes in under a second.

Run:  python3 strategy_runner.py [--dry-run] [--force-close]
Cron: */5 9-15 * * 1-5 cd ... && python3 strategy_runner.py >> trades.log 2>&1
"""

import argparse
import json
import logging
import os
import sys
import time
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from kiteconnect import KiteConnect

# ─── CONFIG ────────────────────────────────────────────────────────────────
API_KEY = os.getenv("API_KEY")
TOKEN_FILE = Path(__file__).parent / "access_token.txt"
LOG_FILE = Path(__file__).parent / "trades.log"
CACHE_DIR = Path(__file__).parent / ".cache"
BUDGET = 15_000
TARGET_PCT = 0.10
RATCHET_TRIGGER_PCT = 0.05
RATCHET_TARGET_PCT = 0.03
LOT_SIZES = {"NIFTY": 65}
OHLC_SYMBOLS = {"NIFTY": "NSE:NIFTY 50"}
STEP = {"NIFTY": 50}
BIG_RED_THRESHOLD = {"NIFTY": 12}
# Broader market stocks for technical exit confirmation
BROADER_MARKET = {
    "NSE:TCS": "TCS", "NSE:INFY": "INFY", "NSE:HDFCBANK": "HDFCBANK",
    "NSE:ICICIBANK": "ICICIBANK", "NSE:SBIN": "SBIN", "NSE:LT": "LT",
    "NSE:AXISBANK": "AXISBANK",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("strategy")


# ─── AUTH ───────────────────────────────────────────────────────────────────
def load_token() -> str:
    if not TOKEN_FILE.exists():
        raise RuntimeError("access_token.txt not found. Run kite_auth.py first.")
    with open(TOKEN_FILE) as f:
        payload = json.load(f)
    if payload.get("date") != str(date.today()):
        raise RuntimeError("Access token is from a previous day. Re-auth needed.")
    return payload["access_token"]


def get_kite() -> KiteConnect:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(load_token())
    return kite


# ─── HELPERS ────────────────────────────────────────────────────────────────
def round_to_tick(price: float) -> float:
    return round(price / 0.05) * 0.05


def dte(expiry: date) -> int:
    return (expiry - date.today()).days


def get_weekday_date(target_weekday: int, after: date = None) -> date:
    """Next date matching target_weekday (0=Mon, 1=Tue, 3=Thu)."""
    if after is None:
        after = date.today()
    days_ahead = target_weekday - after.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return after + timedelta(days=days_ahead)


def get_nifty_expiry() -> tuple[str, date]:
    """Return (token_str, expiry_date) for NIFTY weekly (Thursday)."""
    today = date.today()
    exp = get_weekday_date(3, today)  # next Thursday
    if dte(exp) <= 1:
        exp = get_weekday_date(3, exp + timedelta(days=1))
    token = f"{exp.year % 100}{exp.month}{exp.day}"
    return token, exp


def get_last_tuesday_of_month(year: int, month: int) -> date:
    """Last Tuesday of a given month."""
    import calendar
    _, last_day = calendar.monthrange(year, month)
    d = date(year, month, last_day)
    while d.weekday() != 1:  # Tuesday
        d -= timedelta(days=1)
    return d



# ─── INSTRUMENT CACHE ──────────────────────────────────────────────────────
def load_instrument_cache(kite: KiteConnect, exchange: str = "NFO") -> dict:
    """Load NFO instruments, cache to disk for the day."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"instruments_{exchange}_{date.today()}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)

    instruments = kite.instruments(exchange)
    # Index by tradingsymbol for quick lookup
    idx = {inst["tradingsymbol"]: inst for inst in instruments}
    with open(cache_file, "w") as f:
        json.dump(idx, f, default=str)
    return idx


def find_atm_strike(instruments: dict, root: str, expiry_token: str,
                    index_ltp: float, step_size: int) -> int:
    """Find the ATM strike for a given index LTP."""
    atm = round(index_ltp / step_size) * step_size
    # Verify strike exists
    ce_symbol = f"{root}{expiry_token}{atm}CE"
    pe_symbol = f"{root}{expiry_token}{atm}PE"
    if ce_symbol in instruments:
        return atm
    # If not found, walk up
    for offset in range(step_size, step_size * 10, step_size):
        atm_up = atm + offset
        if f"{root}{expiry_token}{atm_up}CE" in instruments:
            return atm_up
        atm_down = atm - offset
        if f"{root}{expiry_token}{atm_down}CE" in instruments:
            return atm_down
    raise ValueError(f"No strikes found for {root}{expiry_token}")



# ─── MTF QUALITY GATES ─────────────────────────────────────────────────────
def candle_body(ohlc: dict) -> float:
    return abs(ohlc["close"] - ohlc["open"])


def candle_range(ohlc: dict) -> float:
    r = ohlc["high"] - ohlc["low"]
    return r if r > 0 else 0.01


def upper_wick(ohlc: dict) -> float:
    return ohlc["high"] - max(ohlc["close"], ohlc["open"])


def lower_wick(ohlc: dict) -> float:
    return min(ohlc["close"], ohlc["open"]) - ohlc["low"]


def is_gravestone_doji(ohlc: dict) -> bool:
    """Upper wick > 60% of range, body < 30% of range."""
    rng = candle_range(ohlc)
    return upper_wick(ohlc) > 0.6 * rng and candle_body(ohlc) < 0.3 * rng


def is_hammer(ohlc: dict) -> bool:
    """Lower wick > 60% of range, body < 30% of range."""
    rng = candle_range(ohlc)
    return lower_wick(ohlc) > 0.6 * rng and candle_body(ohlc) < 0.3 * rng


def candle_direction(ohlc: dict) -> str:
    """'CE' if bullish (close > open), 'PE' if bearish (close < open), else 'neutral'."""
    if ohlc["close"] > ohlc["open"]:
        return "CE"
    elif ohlc["close"] < ohlc["open"]:
        return "PE"
    return "neutral"


def get_historical(kite: KiteConnect, token: int, interval: str,
                   minutes_back: int) -> list[dict]:
    """Fetch historical candles for a given interval and lookback."""
    end = datetime.now()
    start = end - timedelta(minutes=minutes_back)
    return kite.historical_data(
        instrument_token=token,
        from_date=start.date(),
        to_date=end.date(),
        interval=interval,
        continuous=False,
        oi=False,
    )


def check_mtf(kite: KiteConnect, index_token: int, direction: str,
              index_name: str) -> int:
    """
    Multi-timeframe alignment check.
    Returns number of aligned timeframes (0-3).
    """
    aligned = 0
    session_high = None
    session_low = None

    # 5m (last ~35 min → ~7 candles)
    try:
        candles_5m = get_historical(kite, index_token, "5minute", 40)
        if candles_5m:
            candles_5m = candles_5m[-7:]  # last 7
            # Track session extremes from 5m candles
            session_high = max(c["high"] for c in candles_5m)
            session_low = min(c["low"] for c in candles_5m)

            # Majority in direction
            dir_count = sum(1 for c in candles_5m if candle_direction(c) == direction)
            majority = dir_count >= len(candles_5m) / 2

            # No gravestone at high (for CE) / no hammer at low (for PE)
            if direction == "CE":
                gravestone_ok = not any(
                    is_gravestone_doji(c) and c["high"] >= session_high * 0.998
                    for c in candles_5m
                )
            else:
                gravestone_ok = not any(
                    is_hammer(c) and c["low"] <= session_low * 1.002
                    for c in candles_5m
                )

            if majority and gravestone_ok:
                aligned += 1
    except Exception as e:
        log.warning(f"5m MTF check failed: {e}")

    # 15m (last ~50 min → ~3 candles)
    try:
        candles_15m = get_historical(kite, index_token, "15minute", 55)
        if candles_15m and len(candles_15m) >= 3:
            last_2 = candles_15m[-2:]
            # ≥1 of last 2 in direction
            one_in_dir = any(candle_direction(c) == direction for c in last_2)

            # Exhaustion flag: 3 bodies shrinking AND last < 25% of peak
            bodies = [candle_body(c) for c in candles_15m[-3:]]
            exhaustion = (
                len(bodies) == 3
                and bodies[0] > bodies[1] > bodies[2]
                and bodies[2] < 0.25 * max(bodies)
            )

            if one_in_dir and not exhaustion:
                aligned += 1
    except Exception as e:
        log.warning(f"15m MTF check failed: {e}")

    # 30m (last ~2hr → ~4 candles)
    try:
        candles_30m = get_historical(kite, index_token, "30minute", 130)
        if candles_30m and len(candles_30m) >= 3:
            dirs = [candle_direction(c) for c in candles_30m]
            last_dir = dirs[-1] if dirs else "neutral"

            # Trend intact: last in direction OR ≤1 reversal after ≥2 in-direction
            trend_ok = last_dir == direction
            if not trend_ok and len(dirs) >= 3:
                in_dir_before = sum(1 for d in dirs[:-1] if d == direction)
                trend_ok = in_dir_before >= 2  # ≤1 reversal allowed

            # First reversal at extreme → drop
            if direction == "CE":
                candle_max = max(c["high"] for c in candles_30m)
                reversal_at_extreme = any(
                    candle_direction(c) == "PE" and c["high"] >= candle_max * 0.999
                    for c in candles_30m
                )
                # Upper wick > 50% of range → drop CE
                wick_ok = not any(
                    upper_wick(c) > 0.5 * candle_range(c) for c in candles_30m[-2:]
                )
            else:
                candle_min = min(c["low"] for c in candles_30m)
                reversal_at_extreme = any(
                    candle_direction(c) == "CE" and c["low"] <= candle_min * 1.001
                    for c in candles_30m
                )
                wick_ok = True  # wick check only for CE

            if trend_ok and not reversal_at_extreme and wick_ok:
                aligned += 1
    except Exception as e:
        log.warning(f"30m MTF check failed: {e}")

    return aligned


def check_dead_cat_bounce(kite: KiteConnect, index_token: int,
                          direction: str, index_name: str) -> bool:
    """
    Dead-cat bounce filter. Returns True if trade should be SKIPPED.
    Big reversal candle followed by cumulative recovery > 50% in opposite
    direction across multiple subsequent candles — a bounce inside the
    prior trend, not a new trend.
    """
    try:
        candles = get_historical(kite, index_token, "5minute", 40)
        if not candles or len(candles) < 5:
            return False

        threshold = BIG_RED_THRESHOLD[index_name]
        recent = candles[-6:]

        for i in range(len(recent) - 1):
            body = recent[i]["close"] - recent[i]["open"]

            # Big reversal candle found — sum cumulative recovery across ALL
            # subsequent candles, not just the immediate next one.
            if direction == "CE" and body < -threshold:
                # Big red — sum green candles after, each >= 30% of reversal body
                recovery = sum(
                    max(c["close"] - c["open"], 0) for c in recent[i+1:]
                    if max(c["close"] - c["open"], 0) >= abs(body) * 0.3
                )
                if recovery > abs(body) * 0.5:
                    return True

            elif direction == "PE" and body > threshold:
                # Big green — sum green candles after, each >= 30% of reversal body
                recovery = sum(
                    max(c["close"] - c["open"], 0) for c in recent[i+1:]
                    if max(c["close"] - c["open"], 0) >= body * 0.3
                )
                if recovery > body * 0.5:
                    return True

    except Exception as e:
        log.warning(f"Dead-cat bounce check failed: {e}")

    return False


def compute_proximity(candidate: dict, direction: str) -> float:
    """Proximity to intraday extreme as percentage."""
    ohlc = candidate["ohlc"]
    ltp = candidate["ltp"]
    if direction == "CE":
        return (ohlc["high"] - ltp) / ohlc["high"] * 100
    else:
        return (ltp - ohlc["low"]) / ltp * 100


# ─── SCAN ──────────────────────────────────────────────────────────────────
def scan_indices(kite: KiteConnect, active_indices: set[str]) -> list[dict]:
    """
    Scan all three indices. Return candidates sorted by score (desc).
    Each candidate: {index, direction, momentum, score, ohlc, instrument_token}
    """
    symbols = [s for name, s in OHLC_SYMBOLS.items() if name not in active_indices]
    if not symbols:
        return []

    results = kite.quote(symbols)

    candidates = []
    for index_name, sym in OHLC_SYMBOLS.items():
        if index_name in active_indices:
            continue
        data = results.get(sym)
        if not data or not data.get("ohlc"):
            continue

        ohlc = data["ohlc"]
        ltp = data["last_price"]
        prev_close = ohlc.get("close", ltp)
        open_p = ohlc.get("open", ltp)

        if prev_close == 0:
            continue

        mom = (ltp - prev_close) / prev_close * 100

        if ltp > open_p:
            direction = "CE"
        elif ltp < open_p:
            direction = "PE"
        else:
            continue

        score = abs(mom) if direction else 0

        if 0.3 < abs(mom) < 3.0:
            candidates.append({
                "index": index_name,
                "direction": direction,
                "momentum": mom,
                "score": score,
                "ltp": ltp,
                "ohlc": ohlc,
                "instrument_token": data.get("instrument_token"),
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


# ─── OPTION SELECTION ──────────────────────────────────────────────────────
def select_contract(kite: KiteConnect, instruments: dict, candidate: dict,
                    free_capital: float) -> Optional[dict]:
    """
    Select the best option contract for a candidate signal.
    Returns {symbol, strike, ltp, lot_size, direction} or None.
    """
    index_name = candidate["index"]
    direction = candidate["direction"]
    index_ltp = candidate["ltp"]
    option_root = index_name
    step_size = STEP[index_name]
    default_lot = LOT_SIZES[index_name]

    # Get expiry
    expiry_token, expiry_date = get_nifty_expiry()

    # If computed token not in cache (next-week not listed), pick nearest available
    if not any(option_root + expiry_token in s for s in instruments):
        import re
        available = set()
        for s in instruments:
            m = re.match(rf'{option_root}(\d{{5}})\d+[CP]E', s)
            if m:
                available.add(m.group(1))
        for token in sorted(available):
            try:
                td = date(2000 + int(token[:2]), int(token[2]), int(token[3:5]))
                if td > date.today():
                    expiry_token = token
                    break
            except ValueError:
                continue

    # Find ATM strike
    atm_strike = find_atm_strike(instruments, option_root, expiry_token, index_ltp, step_size)

    # Build candidate symbols: ATM + up to 20 OTM steps (index options)
    max_steps = 20
    strikes = [atm_strike]
    for i in range(1, max_steps + 1):
        if direction == "CE":
            strikes.append(atm_strike + i * step_size)
        else:
            strikes.append(atm_strike - i * step_size)

    # Batch LTP
    symbols_to_check = []
    for strike in strikes:
        sym = f"{option_root}{expiry_token}{strike}{direction}"
        inst = instruments.get(sym)
        if inst:
            symbols_to_check.append(f"NFO:{sym}")

    if not symbols_to_check:
        return None

    try:
        ltp_results = kite.ltp(symbols_to_check)  # batch all candidates
    except Exception as e:
        log.error(f"LTP fetch failed: {e}")
        return None

    for strike in strikes:
        sym = f"{option_root}{expiry_token}{strike}{direction}"
        nfo_key = f"NFO:{sym}"
        inst = instruments.get(sym)

        if not inst:
            continue

        lot_size = inst.get("lot_size", default_lot)
        ltp_data = ltp_results.get(nfo_key)
        if not ltp_data:
            continue

        option_ltp = ltp_data.get("last_price", 0)
        if option_ltp <= 0:
            continue

        # LTP floor: < ₹5 options have wide spreads
        if option_ltp < 5:
            continue

        # Circuit filter
        upper_circuit = inst.get("upper_circuit_limit", float("inf"))
        if option_ltp >= 0.95 * upper_circuit:
            log.info(f"  {sym} LTP={option_ltp} near circuit ({upper_circuit}), skip")
            continue

        lot_cost = option_ltp * lot_size
        if lot_cost <= free_capital:
            log.info(f"  Selected: {sym} LTP={option_ltp} lot_cost={lot_cost:.2f}")
            return {
                "symbol": sym,
                "strike": strike,
                "ltp": option_ltp,
                "lot_size": lot_size,
                "direction": direction,
                "expiry": str(expiry_date),
            }

    return None


# ─── TECHNICAL EXIT ─────────────────────────────────────────────────────────
def check_technical_exit(kite: KiteConnect, position_direction: str) -> bool:
    """
    Dual-confirmation technical exit gate.
    Exit when MTF signals go AGAINST the position:
    - CE (bullish) position → exit on bearish MTF
    - PE (bearish) position → exit on bullish MTF
    """
    try:
        nifty_ltp = kite.ltp(["NSE:NIFTY 50"])
        nifty_token = nifty_ltp["NSE:NIFTY 50"]["instrument_token"]

        # For CE: we score bearish signals (red candles). For PE: bullish (green).
        if position_direction == "CE":
            adverse = "bearish"
        else:
            adverse = "bullish"

        mtf_adverse = 0

        # 5m: majority against position
        c5 = get_historical(kite, nifty_token, "5minute", 40)
        if c5 and len(c5) >= 7:
            if adverse == "bearish":
                adverse_count = sum(1 for c in c5[-7:] if c['close'] < c['open'])
            else:
                adverse_count = sum(1 for c in c5[-7:] if c['close'] > c['open'])
            if adverse_count >= len(c5[-7:]) // 2 + 1:
                mtf_adverse += 1

        # 15m: ≥1 of last 2 against position
        c15 = get_historical(kite, nifty_token, "15minute", 55)
        if c15 and len(c15) >= 2:
            if adverse == "bearish":
                against = sum(1 for c in c15[-2:] if c['close'] < c['open'])
            else:
                against = sum(1 for c in c15[-2:] if c['close'] > c['open'])
            if against >= 1:
                mtf_adverse += 1

        # 30m: last candle against position or wick reversal
        c30 = get_historical(kite, nifty_token, "30minute", 130)
        if c30:
            last = c30[-1]
            rng = last['high'] - last['low']
            upper_w = last['high'] - max(last['close'], last['open'])
            lower_w = min(last['close'], last['open']) - last['low']
            if adverse == "bearish":
                if last['close'] < last['open'] or (rng > 0 and upper_w > 0.5 * rng):
                    mtf_adverse += 1
            else:
                if last['close'] > last['open'] or (rng > 0 and lower_w > 0.5 * rng):
                    mtf_adverse += 1

        if mtf_adverse < 2:
            return False

        # Broader market: >=3 of 7 stocks with majority adverse 5m
        adverse_stock_count = 0
        for sym in BROADER_MARKET:
            try:
                q = kite.ltp([sym])
                tok = q[sym]['instrument_token']
                candles = get_historical(kite, tok, "5minute", 35)
                if candles and len(candles) >= 5:
                    if adverse == "bearish":
                        adverse_c = sum(1 for c in candles[-5:] if c['close'] < c['open'])
                    else:
                        adverse_c = sum(1 for c in candles[-5:] if c['close'] > c['open'])
                    if adverse_c >= 3:
                        adverse_stock_count += 1
            except Exception:
                continue

        return adverse_stock_count >= 3
    except Exception as e:
        log.warning(f"Technical exit check error: {e}")
        return False


# ─── POSITION MANAGEMENT ───────────────────────────────────────────────────
def manage_positions(kite: KiteConnect, dry_run: bool = False) -> set[str]:
    """
    Check open positions: confirm fills, place/ratchet exit orders.
    Returns set of index names with active trades (to block re-entry).
    """
    positions = kite.positions()
    orders = kite.orders()
    active = set()

    open_positions = [p for p in positions.get("net", [])
                      if p["product"] == "MIS" and p["quantity"] != 0]

    if not open_positions:
        # Check if we had completed trades today for same-index blocking
        # Only block on strategy-placed trades (tagged "strategy-runner"), not manual
        completed = [o for o in orders
                     if o.get("status") == "COMPLETE"
                     and o.get("exchange") == "NFO"
                     and o.get("tag") in ("strategy-runner", "strategy-runner-tgt")]
        for idx_name in ("NIFTY",):
            buys = [o for o in completed
                    if o.get("transaction_type") == "BUY"
                    and o.get("tradingsymbol", "").startswith(idx_name)]
            sells = [o for o in completed
                     if o.get("transaction_type") == "SELL"
                     and o.get("tradingsymbol", "").startswith(idx_name)]
            if buys and sells:
                active.add(idx_name)
        return active

    for pos in open_positions:
        symbol = pos["tradingsymbol"]
        qty = abs(pos["quantity"])
        avg = pos.get("average_price", 0)
        if avg <= 0 or qty == 0:
            continue

        # Determine which index
        for idx_name in LOT_SIZES:
            if symbol.startswith(idx_name):
                active.add(idx_name)
                break

        # Check for existing sell order
        pending_sells = [o for o in orders
                         if o.get("tradingsymbol") == symbol
                         and o.get("transaction_type") == "SELL"
                         and o.get("status") in ("OPEN", "PENDING", "TRIGGER PENDING")]

        # Get real LTP
        ltp_data = kite.ltp([f"NFO:{symbol}"])
        ltp = ltp_data.get(f"NFO:{symbol}", {}).get("last_price", pos.get("last_price", 0))

        # Technical exit: MTF moving against position → cancel sell, market exit
        pos_dir = symbol[-2:]  # "CE" or "PE"
        if check_technical_exit(kite, pos_dir):
            adverse_label = "bearish" if pos_dir == "CE" else "bullish"
            log.info(f"  TECH EXIT: {symbol} — NIFTY MTF >=2/3 {adverse_label} + >=3 stocks {adverse_label}")
            # Cancel pending sell
            for sell in pending_sells:
                log.info(f"  Cancel sell: {symbol} order_id={sell['order_id']}")
                if not dry_run:
                    try:
                        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=sell["order_id"])
                        time.sleep(0.3)
                    except Exception as e:
                        log.error(f"  Cancel failed: {e}")
            # Market exit
            exit_price = round_to_tick(ltp * 0.99)
            log.info(f"  TECH EXIT SELL: {symbol} {qty} LIMIT @ {exit_price}")
            if not dry_run:
                try:
                    kite.place_order(
                        variety=kite.VARIETY_REGULAR, exchange="NFO",
                        tradingsymbol=symbol,
                        transaction_type=kite.TRANSACTION_TYPE_SELL,
                        quantity=qty, order_type=kite.ORDER_TYPE_LIMIT,
                        price=exit_price, product=kite.PRODUCT_MIS,
                    )
                except Exception as e:
                    log.error(f"  Tech exit failed: {e}")
            continue

        if pending_sells:
            sell_order = pending_sells[0]
            sell_price = sell_order.get("price", 0)

            # Ratchet check
            ratchet_trigger = avg * (1 + RATCHET_TRIGGER_PCT)
            ratchet_target = round_to_tick(avg * (1 + RATCHET_TARGET_PCT))

            if ltp >= ratchet_trigger and sell_price > ratchet_target:
                log.info(f"  RATCHET: {symbol} LTP={ltp} trigger={ratchet_trigger:.2f} "
                         f"lowering sell {sell_price} → {ratchet_target}")
                if not dry_run:
                    try:
                        kite.cancel_order(
                            variety=kite.VARIETY_REGULAR,
                            order_id=sell_order["order_id"],
                        )
                        time.sleep(0.5)
                        kite.place_order(
                            variety=kite.VARIETY_REGULAR,
                            exchange="NFO",
                            tradingsymbol=symbol,
                            transaction_type=kite.TRANSACTION_TYPE_SELL,
                            quantity=qty,
                            order_type=kite.ORDER_TYPE_LIMIT,
                            price=ratchet_target,
                            product=kite.PRODUCT_MIS,
                        )
                        log.info(f"  Ratchet order placed: {symbol} SELL LIMIT {ratchet_target}")
                    except Exception as e:
                        log.error(f"  Ratchet failed: {e}")
            continue

        # No pending sell — check if we need to place one
        target_price = round_to_tick(avg * (1 + TARGET_PCT))

        if ltp >= target_price:
            # Already at target, market sell
            log.info(f"  EXIT: {symbol} LTP={ltp} >= target={target_price:.2f}, market selling")
            if not dry_run:
                try:
                    kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange="NFO",
                        tradingsymbol=symbol,
                        transaction_type=kite.TRANSACTION_TYPE_SELL,
                        quantity=qty,
                        order_type=kite.ORDER_TYPE_LIMIT,
                        price=round_to_tick(ltp * 0.99),
                        product=kite.PRODUCT_MIS,
                    )
                except Exception as e:
                    log.error(f"  Market exit failed: {e}")
        else:
            # Place resting LIMIT SELL at +10%
            log.info(f"  SELL: {symbol} LIMIT {target_price} (+10% from avg {avg})")
            if not dry_run:
                try:
                    kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange="NFO",
                        tradingsymbol=symbol,
                        transaction_type=kite.TRANSACTION_TYPE_SELL,
                        quantity=qty,
                        order_type=kite.ORDER_TYPE_LIMIT,
                        price=target_price,
                        product=kite.PRODUCT_MIS,
                    )
                except Exception as e:
                    log.error(f"  Sell order failed: {e}")

    return active


# ─── 3 PM FORCE CLOSE ──────────────────────────────────────────────────────
def force_close_all(kite: KiteConnect, dry_run: bool = False):
    """Cancel all pending sell orders and market-close all MIS positions."""
    log.info("=== 3 PM FORCE CLOSE ===")
    orders = kite.orders()
    positions = kite.positions()

    # Cancel pending sell orders
    pending = [o for o in orders if o.get("status") in ("OPEN", "PENDING", "TRIGGER PENDING")
               and o.get("product") == "MIS"]
    for order in pending:
        log.info(f"  Cancel: {order['tradingsymbol']} order_id={order['order_id']}")
        if not dry_run:
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order["order_id"])
            except Exception as e:
                log.error(f"  Cancel failed: {e}")

    time.sleep(1)

    # Market-close all open MIS positions
    open_pos = [p for p in positions.get("net", [])
                if p["product"] == "MIS" and p["quantity"] != 0]
    for pos in open_pos:
        symbol = pos["tradingsymbol"]
        qty = abs(pos["quantity"])
        direction = "SELL" if pos["quantity"] > 0 else "BUY"
        ltp_data = kite.ltp([f"NFO:{symbol}"])
        ltp = ltp_data.get(f"NFO:{symbol}", {}).get("last_price", 0)
        limit_price = round_to_tick(ltp * 0.99) if direction == "SELL" else round_to_tick(ltp * 1.01)

        log.info(f"  Close: {symbol} {direction} {qty} @ LIMIT {limit_price}")
        if not dry_run:
            try:
                kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange="NFO",
                    tradingsymbol=symbol,
                    transaction_type=kite.TRANSACTION_TYPE_SELL if direction == "SELL"
                    else kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=limit_price,
                    product=kite.PRODUCT_MIS,
                )
            except Exception as e:
                log.error(f"  Close failed: {e}")

    # Log net P&L
    log.info("--- Session Summary ---")
    margins = kite.margins()
    net = margins.get("equity", {}).get("net", 0)
    log.info(f"  Account net: ₹{net:.2f}")

    # Sum up day trades
    day_positions = positions.get("day", [])
    for dp in day_positions:
        if dp.get("sell_value", 0) > 0 and dp.get("buy_value", 0) > 0:
            gross = dp["sell_value"] - dp["buy_value"]
            charges = calc_charges(dp["buy_value"], dp["sell_value"])
            net_pnl = gross - charges
            log.info(f"  {dp['tradingsymbol']}: gross={gross:.2f} charges={charges:.2f} net={net_pnl:.2f}")


def calc_charges(buy_value: float, sell_value: float) -> float:
    """Estimate total charges for a round-trip."""
    brokerage = 40
    stt = sell_value * 0.001
    exchange = (buy_value + sell_value) * 0.00053
    stamp = buy_value * 0.00003
    gst = (brokerage + exchange) * 0.18
    return brokerage + stt + exchange + stamp + gst


# ─── MAIN ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Kite Index Options Strategy Runner")
    parser.add_argument("--dry-run", action="store_true", help="Analyze but don't place orders")
    parser.add_argument("--force-close", action="store_true", help="Force-close all positions (3 PM routine)")
    args = parser.parse_args()

    log.info(f"=== Strategy Runner {date.today()} ===")
    if args.dry_run:
        log.info("DRY RUN — no orders will be placed")

    kite = get_kite()

    # 3 PM force close
    now = datetime.now()
    if args.force_close or now.hour >= 15:
        force_close_all(kite, dry_run=args.dry_run)
        return

    # Step 1: Load instrument cache
    instruments = load_instrument_cache(kite)

    # Step 2: Manage existing positions
    active_indices = manage_positions(kite, dry_run=args.dry_run)
    log.info(f"Active/blocked indices: {active_indices}")

    # Step 3: Capital check
    margins = kite.margins()
    positions = kite.positions()
    deployed = sum(
        abs(p["quantity"]) * p.get("average_price", 0)
        for p in positions.get("net", [])
        if p["product"] == "MIS" and p["quantity"] != 0
    )
    free_capital = min(
        margins.get("equity", {}).get("available", {}).get("live_balance", 0),
        max(0, BUDGET - deployed),
    )
    log.info(f"Free capital: ₹{free_capital:.2f} (deployed: ₹{deployed:.2f})")

    if free_capital < 500:
        log.info("Insufficient free capital (< ₹500), skipping new buys")
        return

    # Step 4: Time gate
    if now.hour >= 14 and now.minute >= 30:
        log.info("After 14:30 IST, no new entries")
        return

    is_strict_window = now.hour == 9 and now.minute >= 30 or (
        now.hour == 10 and now.minute <= 15
    )
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        # Before 09:30
        log.info("Before 09:30 IST, no entries (insufficient candles)")
        return

    # Step 5: Scan
    candidates = scan_indices(kite, active_indices)
    if not candidates:
        log.info("No candidates after momentum scan")
        return

    log.info("Candidates: " + str([(c['index'], c['direction'], f"{c['momentum']:.2f}%") for c in candidates]))

    # Step 6: Quality gates
    for candidate in candidates:
        index_name = candidate["index"]
        direction = candidate["direction"]
        index_token = candidate["instrument_token"]

        log.info(f"--- Evaluating {index_name} {direction} (mom={candidate['momentum']:.2f}%) ---")

        # Gate 1: Proximity to extreme
        # Lower prox = closer to extreme = fresher move (good).
        # Fail if pulled back/bounced too far from the intraday extreme.
        prox = compute_proximity(candidate, direction)
        prox_threshold = 0.1 if is_strict_window else 0.5
        if prox > prox_threshold:
            log.info(f"  FAIL: proximity {prox:.2f}% > {prox_threshold}% (pulled back too far from extreme)")
            continue
        log.info(f"  PASS: proximity {prox:.2f}%")

        # Gate 2: MTF
        aligned_count = 0
        if index_token:
            aligned_count = check_mtf(kite, index_token, direction, index_name)
        mtf_required = 3 if is_strict_window else 2
        if aligned_count < mtf_required:
            log.info(f"  FAIL: MTF {aligned_count}/3 < {mtf_required}/3")
            continue
        log.info(f"  PASS: MTF {aligned_count}/3 {'HIGH CONFIDENCE' if aligned_count == 3 else 'standard'}")

        # Gate 3: Dead-cat bounce
        if index_token:
            is_dead_cat = check_dead_cat_bounce(kite, index_token, direction, index_name)
            if is_dead_cat:
                log.info(f"  FAIL: Dead-cat bounce detected")
                continue
            log.info(f"  PASS: No dead-cat bounce")

        # Gate 4: Time gate (already handled above)

        # Step 7: Select contract
        contract = select_contract(kite, instruments, candidate, free_capital)
        if not contract:
            log.info(f"  No suitable contract found for {index_name}")
            continue

        # Step 8: Buy
        buy_price = round_to_tick(contract["ltp"])
        qty = contract["lot_size"]
        log.info(f"  BUY: {contract['symbol']} {qty} qty LIMIT @ {buy_price}")

        if args.dry_run:
            log.info("  [DRY RUN] Order not placed")
            return

        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange="NFO",
                tradingsymbol=contract["symbol"],
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=buy_price,
                product=kite.PRODUCT_MIS,
                tag="strategy-runner",
            )
            log.info(f"  Buy order placed: {order_id}")

            # Confirm fill
            time.sleep(1)
            order_status = kite.orders()
            buy_order = next(
                (o for o in order_status if o["order_id"] == order_id), None
            )
            if buy_order and buy_order.get("status") == "COMPLETE":
                avg_price = buy_order.get("average_price", contract["ltp"])
                log.info(f"  FILLED at avg {avg_price}")

                # Place sell
                sell_price = round_to_tick(avg_price * (1 + TARGET_PCT))
                sell_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange="NFO",
                    tradingsymbol=contract["symbol"],
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=sell_price,
                    product=kite.PRODUCT_MIS,
                    tag="strategy-runner-tgt",
                )
                log.info(f"  SELL order placed: {sell_id} LIMIT @ {sell_price} (+10%)")
            else:
                log.warning(f"  Buy not filled yet, status: {buy_order.get('status') if buy_order else 'unknown'}")
        except Exception as e:
            log.error(f"  Buy failed: {e}")
            continue  # try next candidate

        # Only one trade per run
        break


if __name__ == "__main__":
    main()
