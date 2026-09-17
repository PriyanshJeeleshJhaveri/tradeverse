# TradeVerse

TradeVerse is a Flask paper-trading dashboard with SQLite user authentication and TinyMongo/TinyDB JSON portfolio storage.

## Vercel deployment

This version is prepared for Vercel's Flask runtime. Vercel can detect a Flask `app` from a supported entry file such as the root `app.py`, so no custom Python function entrypoint is required.

### 1. Put the project in the Vercel project root

The deployable folder should contain:

- `app.py`
- `portfolio_db.py`
- `price_service.py`
- `storage_paths.py`
- `templates/`
- `public/static/`
- `tradeverse.db`
- `tinydb_storage/tradeverse.json`
- `requirements.txt`
- `vercel.json`

### 2. Configure Vercel Environment Variables

Add these in the Vercel project settings:

```text
FLASK_SECRET_KEY=<long-random-secret>
TWELVEDATA_API_KEY=<your-twelve-data-key>
COINGECKO_API_KEY=<your-coingecko-key-if-used>
PRICE_CACHE_TTL_SECONDS=600
REQUEST_TIMEOUT_SECONDS=5
TRADEVERSE_BOOTSTRAP_ADMIN=0
```

Do **not** upload a real `.env` file. `.env` is intentionally excluded from the deployment package. Use `.env.example` for local configuration guidance.

### 3. Deploy

You can connect the repository to Vercel or deploy with the Vercel CLI. For local Vercel-style testing:

```bash
npm i -g vercel
vercel dev
```

For production:

```bash
vercel deploy --prod
```

## Storage behavior on Vercel

The existing storage files are deliberately **not migrated or replaced**:

- `tradeverse.db` remains the SQLite database.
- `tinydb_storage/tradeverse.json` remains the TinyMongo/TinyDB JSON database.
- The existing JSON document structure is preserved.

There is one unavoidable Vercel limitation: the deployed project filesystem is read-only and Vercel Function storage is ephemeral. `storage_paths.py` therefore copies the bundled `tradeverse.db` and `tradeverse.json` into `/tmp/tradeverse` when running on Vercel and performs runtime writes there. This prevents SQLite/TinyDB write errors while preserving the original files and format.

**Important:** those `/tmp` writes are not durable shared storage. A new Vercel function instance can start again from the bundled database/JSON seed. This means registration and portfolio changes should be treated as instance-local on Vercel. If TradeVerse needs durable multi-user writes in production, the persistence layer will eventually need a remote durable database/storage service. The application code does not perform that migration in this version.

## Local development

Locally, the application continues to use the original `tradeverse.db` and `tinydb_storage/tradeverse.json` directly.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Security notes

- Set a strong `FLASK_SECRET_KEY` in Vercel.
- Keep API keys in Vercel Environment Variables rather than source control.
- The bundled database currently contains the project's existing demo/admin account. Change or remove that account before using the deployment for anything beyond a demo.
- `TRADEVERSE_BOOTSTRAP_ADMIN=0` prevents the application from creating a known default admin account automatically.

## Current application scope

TradeVerse currently supports authentication, market search, live quotes, charts, wallets, portfolio holdings, and profit/loss display. Buy, sell, and limit-order buttons remain UI placeholders; no order-execution API has been added here.
