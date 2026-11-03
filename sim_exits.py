"""Run the bot's real exit functions against a fake Kite account, with dry_run=True.

Uses the 13 May 2026 trade: NIFTY2651923600CE, 65 qty, filled at 221.60.
"""

import sys

import strategy_runner as sr

sr.time.sleep = lambda s: None  # no waiting in the simulation

SYMBOL = "NIFTY2651923600CE"
AVG = 221.6
QTY = 65


def candles(n, green):
    if green:
        return [{"open": 100, "high": 106, "low": 99, "close": 105}] * n
    return [{"open": 105, "high": 106, "low": 99, "close": 100}] * n


class FakeKite:
    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_SELL = "SELL"
    TRANSACTION_TYPE_BUY = "BUY"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_MIS = "MIS"

    def __init__(self, option_ltp, pending_sell=None, market_green=True, day=None):
        self.option_ltp = option_ltp
        self.pending_sell = pending_sell
        self.market_green = market_green
        self.day = day or []

    def positions(self):
        net = [{"tradingsymbol": SYMBOL, "product": "MIS", "quantity": QTY,
                "average_price": AVG, "last_price": self.option_ltp}]
        return {"net": net, "day": self.day}

    def orders(self):
        if self.pending_sell is None:
            return []
        return [{"order_id": "260513171160362", "tradingsymbol": SYMBOL,
                 "transaction_type": "SELL", "status": "OPEN", "product": "MIS",
                 "price": self.pending_sell}]

    def ltp(self, symbols):
        out = {}
        for s in symbols:
            price = self.option_ltp if s == f"NFO:{SYMBOL}" else 23500
            out[s] = {"last_price": price, "instrument_token": 256265}
        return out

    def historical_data(self, **kwargs):
        return candles(8, self.market_green)

    def margins(self):
        return {"equity": {"net": 50000.00}}


def run(title, kite, fn):
    print(f"\n### {title}")
    fn(kite, dry_run=True)


scenario = sys.argv[1]
if scenario == "target":
    run("Just filled, no sell order yet, LTP 223.00",
        FakeKite(223.00), sr.manage_positions)
elif scenario == "ratchet":
    run("Sell resting at 243.75, LTP 233.00",
        FakeKite(233.00, pending_sell=243.75), sr.manage_positions)
elif scenario == "techexit":
    run("Sell resting at 243.75, LTP 190.40, market turning red",
        FakeKite(190.40, pending_sell=243.75, market_green=False), sr.manage_positions)
elif scenario == "forceclose":
    run("3 PM, sell resting at 243.75, LTP 230.00",
        FakeKite(230.00, pending_sell=243.75), sr.force_close_all)
