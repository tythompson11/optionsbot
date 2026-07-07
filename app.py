"""
TradingView -> Options Signal -> Discord bot
---------------------------------------------
Signal-only. No trade execution. No Alpaca dependency.

Flow:
  1. TradingView Pine Script alert fires -> POSTs JSON to /webhook
  2. This app validates the symbol against WATCHLIST
  3. yfinance is used to find the nearest Friday expiry + ATM contract
  4. Stop loss / take profit are computed as a % of the option premium
  5. A formatted signal is posted to Discord via webhook
"""

import os
import logging
from datetime import datetime, timedelta

import requests
import yfinance as yf
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("optionsbot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # optional shared secret from TradingView alert body

DEFAULT_STOP_PCT = float(os.getenv("DEFAULT_STOP_PCT", "30"))    # stop loss = -30% of premium
DEFAULT_TARGET_PCT = float(os.getenv("DEFAULT_TARGET_PCT", "50"))  # take profit = +50% of premium

# Ticker aliases: what TradingView calls a symbol -> what yfinance expects
TICKER_ALIASES = {
    "SPX": "^SPX",     # S&P 500 index (cash-settled options chain is on ^SPX in yfinance)
}

WATCHLIST = {
    "AMD", "INTC", "NVDA", "QQQ", "SPY",
    "MSFT", "AMZN", "GOOGL", "META", "SPX",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def yf_ticker_for(symbol: str) -> str:
    return TICKER_ALIASES.get(symbol, symbol)


def nearest_friday_expiry(available_expiries):
    """Pick the nearest available expiry that falls on/after today, preferring Fridays."""
    today = datetime.utcnow().date()
    parsed = sorted(datetime.strptime(e, "%Y-%m-%d").date() for e in available_expiries)
    future = [d for d in parsed if d >= today]
    if not future:
        raise ValueError("No future expiries available")

    fridays = [d for d in future if d.weekday() == 4]
    chosen = fridays[0] if fridays else future[0]
    return chosen.strftime("%Y-%m-%d")


def get_atm_contract(symbol: str, direction: str):
    """
    direction: 'call' or 'put'
    Returns dict with strike, expiry, last_price, bid, ask, contract_symbol
    """
    yf_symbol = yf_ticker_for(symbol)
    tk = yf.Ticker(yf_symbol)

    expiries = tk.options
    if not expiries:
        raise ValueError(f"No options data available for {symbol}")

    expiry = nearest_friday_expiry(expiries)

    chain = tk.option_chain(expiry)
    table = chain.calls if direction == "call" else chain.puts

    spot_hist = tk.history(period="1d")
    if spot_hist.empty:
        raise ValueError(f"No spot price data for {symbol}")
    spot_price = float(spot_hist["Close"].iloc[-1])

    table = table.copy()
    table["diff"] = (table["strike"] - spot_price).abs()
    row = table.sort_values("diff").iloc[0]

    premium = float(row["lastPrice"])
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    # prefer mid of bid/ask if both are live, otherwise fall back to lastPrice
    if bid > 0 and ask > 0:
        premium = round((bid + ask) / 2, 2)

    return {
        "expiry": expiry,
        "strike": float(row["strike"]),
        "premium": premium,
        "bid": bid,
        "ask": ask,
        "contract_symbol": row.get("contractSymbol", ""),
        "spot_price": spot_price,
    }


def compute_stop_target(premium: float, stop_pct: float, target_pct: float):
    stop_loss = round(premium * (1 - stop_pct / 100), 2)
    take_profit = round(premium * (1 + target_pct / 100), 2)
    return stop_loss, take_profit


def post_to_discord(payload: dict):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    direction = payload["direction"].upper()
    emoji = "🟢" if direction == "CALL" else "🔴"
    option_word = "CALL" if direction == "CALL" else "PUT"

    # Plain-language expiry, e.g. "2026-07-10" -> "Jul 10, 2026"
    expiry_dt = datetime.strptime(payload["expiry"], "%Y-%m-%d")
    expiry_readable = expiry_dt.strftime("%b %d, %Y")

    description = (
        f"**What to buy:** {payload['symbol']} ${payload['strike']} {option_word}, "
        f"expires {expiry_readable}\n"
        f"**Buy at:** ~${payload['premium']} per contract\n\n"
        f"🛑 **Sell if price drops to:** ${payload['stop_loss']}  _(cuts your loss)_\n"
        f"🎯 **Sell if price rises to:** ${payload['take_profit']}  _(locks in your profit)_"
    )

    embed = {
        "title": f"{emoji} {payload['symbol']} — BUY A {option_word} OPTION",
        "description": description,
        "color": 3066993 if direction == "CALL" else 15158332,
        "footer": {"text": "Signal only — you place this trade yourself. Not financial advice."},
        "timestamp": datetime.utcnow().isoformat(),
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "invalid or missing JSON body"}), 400

    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    symbol = normalize_symbol(data.get("symbol", ""))
    action = str(data.get("action", "")).lower()  # e.g. "buy_call", "buy_put", "long", "short"

    if symbol not in WATCHLIST:
        log.info("Ignoring alert for symbol not in watchlist: %s", symbol)
        return jsonify({"status": "ignored", "reason": "symbol not in watchlist"}), 200

    if action in ("buy_call", "long", "call"):
        direction = "call"
    elif action in ("buy_put", "short", "put"):
        direction = "put"
    else:
        return jsonify({"error": f"unrecognized action '{action}'"}), 400

    stop_pct = float(data.get("stop_pct", DEFAULT_STOP_PCT))
    target_pct = float(data.get("target_pct", DEFAULT_TARGET_PCT))

    try:
        contract = get_atm_contract(symbol, direction)
    except Exception as e:
        log.exception("Failed to fetch option contract for %s", symbol)
        return jsonify({"error": str(e)}), 500

    stop_loss, take_profit = compute_stop_target(contract["premium"], stop_pct, target_pct)

    payload = {
        "symbol": symbol,
        "direction": direction,
        "expiry": contract["expiry"],
        "strike": contract["strike"],
        "premium": contract["premium"],
        "spot_price": contract["spot_price"],
        "contract_symbol": contract["contract_symbol"],
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }

    try:
        post_to_discord(payload)
    except Exception as e:
        log.exception("Failed to post to Discord")
        return jsonify({"error": f"discord post failed: {e}"}), 500

    return jsonify({"status": "sent", "signal": payload}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "watchlist": sorted(WATCHLIST)}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)