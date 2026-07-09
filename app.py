"""
TradingView -> Options Signal -> Discord bot
---------------------------------------------
Signal-only. No trade execution. No Alpaca dependency.

Flow:
  1. TradingView Pine Script alert fires -> POSTs JSON to /webhook
  2. This app validates the symbol against WATCHLIST
  3. Tradier (real-time market data) is used to find the nearest Friday
     expiry + ATM contract
  4. Stop loss / take profit are computed as a % of the option premium
  5. A formatted signal is posted to Discord via webhook
"""

import os
import logging
from datetime import datetime

import requests
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

TRADIER_API_KEY = os.getenv("TRADIER_API_KEY")
TRADIER_BASE_URL = "https://api.tradier.com/v1"  # production = real-time data

WATCHLIST = {
    "AMD", "INTC", "NVDA", "QQQ", "SPY",
    "MSFT", "AMZN", "GOOGL", "META", "SPX",
}

# ---------------------------------------------------------------------------
# Tradier helpers
# ---------------------------------------------------------------------------

def tradier_get(path: str, params: dict):
    if not TRADIER_API_KEY:
        raise RuntimeError("TRADIER_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {TRADIER_API_KEY}",
        "Accept": "application/json",
    }
    resp = requests.get(f"{TRADIER_BASE_URL}{path}", headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_spot_price(symbol: str) -> float:
    data = tradier_get("/markets/quotes", {"symbols": symbol})
    quote = data.get("quotes", {}).get("quote")
    if quote is None:
        raise ValueError(f"No quote data for {symbol}")
    if isinstance(quote, list):
        quote = quote[0]
    price = quote.get("last") or quote.get("close")
    if price is None:
        raise ValueError(f"No usable price field for {symbol}")
    return float(price)


def get_expirations(symbol: str):
    data = tradier_get("/markets/options/expirations", {"symbol": symbol})
    dates = data.get("expirations")
    if not dates:
        return []
    dates = dates.get("date")
    if dates is None:
        return []
    if isinstance(dates, str):
        return [dates]
    return dates


def get_option_chain(symbol: str, expiry: str):
    data = tradier_get(
        "/markets/options/chains",
        {"symbol": symbol, "expiration": expiry, "greeks": "false"},
    )
    options = data.get("options")
    if not options:
        return []
    options = options.get("option")
    if options is None:
        return []
    if isinstance(options, dict):
        return [options]
    return options


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


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
    Returns dict with strike, expiry, premium, bid, ask, contract_symbol, spot_price
    """
    expiries = get_expirations(symbol)
    if not expiries:
        raise ValueError(f"No options data available for {symbol}")

    expiry = nearest_friday_expiry(expiries)

    chain = get_option_chain(symbol, expiry)
    if not chain:
        raise ValueError(f"No option chain returned for {symbol} {expiry}")

    option_type = "call" if direction == "call" else "put"
    candidates = [o for o in chain if o.get("option_type") == option_type]
    if not candidates:
        raise ValueError(f"No {option_type} contracts found for {symbol} {expiry}")

    spot_price = get_spot_price(symbol)

    best = min(candidates, key=lambda o: abs(float(o["strike"]) - spot_price))

    bid = float(best.get("bid") or 0)
    ask = float(best.get("ask") or 0)
    last = float(best.get("last") or 0)

    if bid > 0 and ask > 0:
        premium = round((bid + ask) / 2, 2)
    elif last > 0:
        premium = round(last, 2)
    else:
        raise ValueError(f"No valid pricing data for {symbol} contract")

    return {
        "expiry": expiry,
        "strike": float(best["strike"]),
        "premium": premium,
        "bid": bid,
        "ask": ask,
        "contract_symbol": best.get("symbol", ""),
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
