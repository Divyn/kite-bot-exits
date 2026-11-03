# Kite Bot Exits

A Python trading bot on the Zerodha Kite Connect API with four exits, so it never holds an intraday position into Zerodha's auto square-off:

1. **Profit target.** A limit sell at 10% above the fill price, placed right after the buy fills.
2. **Ratchet.** If the price climbs 5% above the fill, the sell moves down to 3% above the fill, once.
3. **Technical exit.** When NIFTY's 5, 15 and 30 minute candles (at least 2 of 3) and at least 3 of 7 large stocks turn against the position, the bot cancels the target and sells at 1% below the last price.
4. **3 PM force close.** Any run at 3 PM or later cancels every open intraday order and closes every intraday position.

This is a technical tutorial only. It is not investment advice.

## Files

| File | What it does |
| --- | --- |
| `strategy_runner.py` | The bot. The exits are in `check_technical_exit`, `manage_positions` and `force_close_all`. |
| `auth.py` | Logs in to Kite once a day and saves `access_token.txt`. |
| `sim_exits.py` | Runs the real exit functions against a fake account in dry run mode. No Kite account needed. |

## Try the exits without an account

```bash
pip install -r requirements.txt
python3 sim_exits.py target
python3 sim_exits.py ratchet
python3 sim_exits.py techexit
python3 sim_exits.py forceclose
```

Each run uses one example trade: 65 units of a NIFTY call filled at 221.60. Dry run means the bot logs what it would do and sends no order.

## Run it for real

1. Create an app on the [Kite developer console](https://developers.kite.trade) and add your server's IP address to its allowed IPs. Kite rejects orders from any other IP.
2. Copy `.env.example` to `.env` and fill in `API_KEY` and `API_SECRET`.
3. Each morning, run `python3 auth.py` and log in.
4. Export `API_KEY` in the shell or cron line, then add the cron job from the top of `strategy_runner.py`. It runs every 5 minutes in market hours.

Start with `python3 strategy_runner.py --dry-run`.
