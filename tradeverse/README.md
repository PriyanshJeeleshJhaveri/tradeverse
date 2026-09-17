# TradeVerse - Paper Trading Platform (Starter)

Flask + SQLite + TinyMongo starter for the TradeVerse multi-asset paper trading
platform. Currently includes: Login, Create Account, and the main Dashboard
(portfolio viewer + market search). Buying/selling assets is not wired up yet.

## Tech stack
- Backend: Python (Flask)
- Auth database: SQLite (`tradeverse.db`) — stores username, email, phone, password (hashed)
- Portfolio/wallet database: NoSQL, via **tinymongo** (a MongoDB-style wrapper
  around TinyDB) — stores wallet balances + ISE/USE/CCME holdings, in
  `tinydb_storage/tradeverse.json`
- Live prices: direct HTTP calls (via `requests`) to
  - **Yahoo Finance's chart endpoint** for stocks & indices
  - **CoinGecko's REST API** for crypto
  No `yfinance` library involved. Cached for **10 minutes** per symbol.
- Config: `.env` file (see `.env.example`), loaded with `python-dotenv`
- Frontend: HTML, CSS, JS (same 2016/2017-style look as before — unchanged in this update)

## Configuration (.env)
Copy `.env.example` to `.env` and adjust if needed — every setting is optional:
```
FLASK_SECRET_KEY=...            # Flask session secret
COINGECKO_API_KEY=...           # optional CoinGecko demo/pro key
PRICE_CACHE_TTL_SECONDS=600     # price cache lifetime, default 10 min
REQUEST_TIMEOUT_SECONDS=10      # HTTP timeout for price lookups
YAHOO_USER_AGENT=...            # browser UA sent to Yahoo Finance
```
A working `.env` with sensible defaults is already included, so the app runs
out of the box without any setup.

## How to run

1. (Optional) create a virtual environment:
   ```
   python3 -m venv venv
   source venv/bin/activate      # on Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Run the app:
   ```
   python app.py
   ```

4. Open `http://127.0.0.1:5000` in your browser.

   `tradeverse.db` (SQLite) and `tinydb_storage/` (portfolios) are created
   automatically on first run, along with the default admin account below.

## Default admin account
```
username: admin
password: admin@123
email:    admin@gmail.com
phone:    1234567890
```
The admin account comes pre-loaded with a sample portfolio (a few Indian
stocks, US stocks, and crypto) so the dashboard has something to show right
away. Its wallet balances reflect having already "spent" part of the default
starting funds on that sample portfolio.

## What happens when a new user registers
Every new account starts with **empty** ISE/USE/CCME portfolios and these
wallet balances:
- ISE (Indian Stock Market): ₹10,00,000
- USE (US Stock Market): $10,000
- CCME (Crypto Currency Market Exchange): $10,000

## Portfolio data model (tinymongo / NoSQL)
One document per user in the `portfolios` collection:
```json
{
  "user_id": 1,
  "username": "admin",
  "wallet": { "ISE": 895250.0, "USE": 2100.0, "CCME": 1750.0 },
  "ISE_portfolio":  [ { "name": "RELIANCE.NS", "bought_price": 2400.0, "price": 2550.0, "quantity": 20, "total_amount": 51000.0 } ],
  "USE_portfolio":  [ { "name": "AAPL", "bought_price": 180.0, "price": 195.0, "quantity": 15, "total_amount": 2925.0 } ],
  "CCME_portfolio": [ { "name": "BTC-USD", "bought_price": 45000.0, "price": 61000.0, "quantity": 0.1, "total_amount": 6100.0 } ]
}
```
- ISE prices & transactions are stored/shown in **INR**
- USE and CCME prices & transactions are stored/shown in **USD**
- `price` is the live current price (refreshed via yfinance); `total_amount = price * quantity`

## Pages
- **`/dashboard`** — landing page: hamburger dropdown (links to each market's
  page), a decorative search bar (feature removed for now, bar kept for
  layout), a live Bitcoin price hero box, and the S&P 500 / NIFTY 50 / SENSEX
  index boxes.
- **`/market/ISE`** — Indian Stock Market: wallet balance + holdings table
- **`/market/USE`** — US Stock Exchange: wallet balance + holdings table
- **`/market/CCME`** — Crypto Currency Market Exchange: wallet balance + holdings table

Each market page has its own hamburger dropdown so you can jump straight to
another market's page.

Prices refresh automatically **every 10 minutes**, both server-side
(in-memory cache in `price_service.py`) and client-side (JS `setInterval`).

## Notes / known limitations
- Buying and selling assets is not implemented yet — these are read-only
  pages for now, as requested.
- The search bar on the dashboard is currently just a visual element — the
  live search feature was removed at the user's request but the bar itself
  was kept in the layout.
- Yahoo Finance's chart endpoint and CoinGecko's API are both public/unofficial
  in the sense that no paid subscription is required, but they can rate-limit
  or block traffic that looks automated. If a lookup fails, the app falls
  back to the last cached price instead of crashing. If you keep seeing
  `Unavailable`, check your outbound internet access to
  `query1.finance.yahoo.com` and `api.coingecko.com`.
- "SENSEX 100" from the original wireframe isn't a real published index, so
  the SENSEX box uses the actual BSE SENSEX (`^BSESN`) instead.
