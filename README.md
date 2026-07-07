# TradingView → Options Signal → Discord Bot

Signal-only bot. It does **not** place trades. It receives TradingView alerts,
looks up the nearest ATM option contract + Friday expiry via `yfinance`, and
posts a formatted buy-call / buy-put signal to Discord with a stop loss and
take profit.

This is fully independent from any Alpaca-based trading bot — no shared
credentials, no shared code path, no order execution of any kind.

## Watchlist

```
AMD, INTC, NVDA, QQQ, SPY, MSFT, AMZN, GOOGL, META, SPX
```

Edit the `WATCHLIST` set in `app.py` to add/remove symbols. SPX is mapped
internally to `^SPX` for yfinance's options chain lookup — you don't need to
do anything special in TradingView, just send `"symbol": "SPX"`.

## 1. Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste in your Discord webhook URL
```

### Create a Discord webhook
Server Settings → Integrations → Webhooks → New Webhook → copy URL → paste
into `.env` as `DISCORD_WEBHOOK_URL`.

### Run locally
```bash
python app.py
```
Server starts on `http://localhost:5000`. Health check: `GET /health`.

## 2. TradingView Alert Setup

In your Pine Script strategy/indicator, create an alert whose **message**
is a JSON body matching this schema, sent to your webhook's `/webhook` URL.

### Buy Call example
```json
{
  "secret": "change_me_to_something_random",
  "symbol": "AMD",
  "action": "buy_call",
  "stop_pct": 30,
  "target_pct": 50
}
```

### Buy Put example
```json
{
  "secret": "change_me_to_something_random",
  "symbol": "NVDA",
  "action": "buy_put",
  "stop_pct": 25,
  "target_pct": 60
}
```

**Fields:**
- `symbol` — must be one of the watchlist tickers above
- `action` — one of: `buy_call` / `long` / `call` → CALL, `buy_put` / `short` / `put` → PUT
- `stop_pct` (optional) — stop loss as % below entry premium. Defaults to `DEFAULT_STOP_PCT` in `.env`
- `target_pct` (optional) — take profit as % above entry premium. Defaults to `DEFAULT_TARGET_PCT`
- `secret` (optional) — only required if you set `WEBHOOK_SECRET` in `.env`

You can hardcode `stop_pct` / `target_pct` per-alert in Pine using
`str.tostring()` if you want different risk parameters per strategy, e.g.:

```pinescript
alertMessage = '{"secret":"change_me_to_something_random","symbol":"AMD","action":"buy_call","stop_pct":' + str.tostring(myStopPct) + ',"target_pct":' + str.tostring(myTargetPct) + '}'
alert(alertMessage, alert.freq_once_per_bar_close)
```

## 3. What the bot does with each alert

1. Confirms the symbol is in the watchlist (anything else is silently ignored)
2. Pulls the options chain for that symbol via `yfinance`
3. Picks the nearest expiry that lands on a Friday (falls back to nearest
   available expiry if no Friday exists in the near term)
4. Finds the strike closest to the current spot price (ATM)
5. Computes stop loss / take profit as a % of the option premium (mid of
   bid/ask, or last traded price if quotes are stale)
6. Posts a formatted embed to your Discord channel

## 4. Deployment Options

### Option A — ngrok (local testing only)
```bash
python app.py
ngrok http 5000
```
Use the `https://xxxx.ngrok.io/webhook` URL as your TradingView alert
webhook URL. Good for testing; the tunnel dies when your machine sleeps.

### Option B — Render (recommended for always-on, low effort)
1. Push this folder to a GitHub repo
2. Render → New → Web Service → connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python app.py`
5. Add environment variables from `.env` in Render's dashboard
6. Use the resulting `https://your-app.onrender.com/webhook` as your alert URL

### Option C — Railway
1. Push to GitHub, then Railway → New Project → Deploy from repo
2. Add environment variables in Railway's dashboard
3. Railway auto-detects the Flask app; ensure start command is `python app.py`
4. Use the generated public URL + `/webhook`

## 5. Notes & Limitations

- **yfinance data is delayed/free-tier quality**, not real-time. For a
  signal-only bot this is generally fine, but don't expect NBBO-accurate
  premiums.
- **SPX options are cash-settled and European-style** — the ATM lookup
  logic is the same, but be aware of the different exercise mechanics if
  you act on these signals manually.
- This bot **never touches your Alpaca account** or any brokerage API —
  it only reads public market data and posts to Discord.
- Since there's no execution, "stop loss" and "take profit" here are just
  numbers displayed in the signal for you to act on manually.
