# TradeVerse — Flask + MongoDB Atlas + Vercel

TradeVerse is a Flask application deployed on Vercel with MongoDB Atlas as the
persistent application database.

## What was changed

The previous version used SQLite for accounts and MongoDB only for portfolios.
That is not durable on Vercel because Vercel Functions do not provide a
persistent writable application filesystem. This version moves **users,
authentication data, wallets, and portfolios into MongoDB Atlas**.

The application now provides this flow:

```text
Register account
    ↓
MongoDB users collection
    ↓
Create user's default portfolio
    ↓
Login
    ↓
USER → /dashboard
ADMIN → /admin
```

On startup, if `ADMIN_USERNAME`, `ADMIN_EMAIL`, `ADMIN_PHONE`, and
`ADMIN_PASSWORD` are configured and the configured username does not already
exist, TradeVerse creates the first `ADMIN` account and its **Admin Portfolio**.
The admin password is not hard-coded and is not overwritten on later deploys.

The default administrator portfolio preserves the existing demo positions and
wallet balances from the supplied project.

## MongoDB collections

### users

```json
{
  "id": "...",
  "username": "admin",
  "username_normalized": "admin",
  "email": "admin@example.com",
  "phone": "...",
  "password_hash": "...",
  "role": "ADMIN",
  "created_at": "...",
  "updated_at": "..."
}
```

### portfolios

```json
{
  "user_id": "...",
  "username": "admin",
  "portfolio_name": "Admin Portfolio",
  "description": "Default portfolio for the administrator.",
  "is_default": true,
  "is_admin_portfolio": true,
  "wallet": {
    "ISE": 1000000.0,
    "USE": 10000.0,
    "CCME": 10000.0
  },
  "ISE_portfolio": [],
  "USE_portfolio": [],
  "CCME_portfolio": []
}
```

## Local setup

1. Create a Python virtual environment.
2. Install dependencies.
3. Copy `.env.example` to `.env`.
4. Put your MongoDB Atlas connection string in `MONGODB_URI`.
5. Generate a strong `FLASK_SECRET_KEY`.
6. Set a strong, unique `ADMIN_PASSWORD`.
7. Start Flask.

```bash
python -m venv .venv

# Windows PowerShell
.venv\\Scripts\\Activate.ps1

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# Copy .env.example to .env and fill the values.
python app.py
```

Open `http://127.0.0.1:5000`.

## Vercel setup

Vercel currently supports Flask apps through its Python runtime and detects a
Flask `app` entry point such as `app.py`. Static assets should live under
`public/**`, which this project now uses. citehttps://vercel.com/docs/frameworks/backend/flask

### 1. Push the updated project to GitHub

Commit and push the files in this ZIP to your public repository.

### 2. Import the repository into Vercel

Create/import the Vercel project and set its Root Directory to the folder that
contains `app.py` if the repository has another parent folder.

### 3. Add Vercel Environment Variables

In **Vercel → Project → Settings → Environment Variables**, add at least:

```text
FLASK_SECRET_KEY
MONGODB_URI
MONGODB_DB_NAME
MONGODB_USERS_COLLECTION
MONGODB_PORTFOLIO_COLLECTION
ADMIN_USERNAME
ADMIN_EMAIL
ADMIN_PHONE
ADMIN_PASSWORD
```

Environment variable changes apply to new deployments, so redeploy after
changing them. citehttps://vercel.com/docs/environment-variables/managing-environment-variables

### 4. Configure MongoDB Atlas networking

The IP address you added from your own computer is sufficient for local
connections, but Vercel deployments do not use that same client IP.
MongoDB's Vercel integration documentation states that Vercel deployments use
dynamic IP addresses; without a dedicated/static networking arrangement, Atlas
must permit the Vercel deployment addresses, commonly by allowing `0.0.0.0/0`.
Use the narrowest networking option available for your Vercel/Atlas setup and
understand that `0.0.0.0/0` permits connections from all IPv4 addresses; the
MongoDB database username/password still protect database authentication.
citehttps://www.mongodb.com/docs/atlas/reference/partner-integrations/vercel/

### 5. Deploy

A Git-connected Vercel project deploys from pushes to the configured production
branch. You can also use the Vercel CLI:

```bash
vercel link
vercel env pull .env.local
vercel deploy
vercel deploy --prod
```

Vercel documents these commands for linking, pulling environment variables,
and deploying projects from the CLI. citehttps://vercel.com/docs/projects/deploy-from-cli

## First-run behavior

When the Vercel function starts with a valid `MONGODB_URI`, it creates the
required indexes and attempts to bootstrap the configured admin. If the admin
already exists, its password is not replaced. If the admin portfolio already
exists, the existing data is preserved.

### Default admin login

There is **no hard-coded public password** in the source code.
Use the values you choose for:

```text
ADMIN_USERNAME
ADMIN_EMAIL
ADMIN_PHONE
ADMIN_PASSWORD
```

After the first successful bootstrap, log in using that username/password.
Admins are sent to `/admin`; normal users are sent to `/dashboard`.

## Security notes

- Never commit `.env` or real connection strings.
- Never put `MONGODB_URI`, database passwords, API keys, or `FLASK_SECRET_KEY`
in a public repository.
- Use a unique strong admin password.
- Rotate any MongoDB password that has already been exposed in screenshots,
chat messages, or commits.
- MongoDB database users are separate from Atlas UI users. citehttps://www.mongodb.com/docs/atlas/connect-to-database-deployment/
- The Atlas database user created during first-cluster setup may initially have broader permissions than this application needs. After confirming the app works, reduce that database user to the minimum database role required for the `tradeverse` database (for example, read/write access to the application database), rather than leaving Atlas-wide administrative permissions.
- Application `ADMIN`/`USER` roles are stored in TradeVerse's `users` collection;
they are unrelated to Atlas organization/database permissions.

## Routes

```text
/                 Home redirect
/health           MongoDB/Vercel deployment health check
/register         Create a user account
/login            Login
/logout           Logout
/dashboard        Normal user dashboard
/market/<market>  Portfolio/wallet UI
/asset/...        Asset detail page
/admin            Admin dashboard
/admin/portfolio  Admin default portfolio editor
/admin/users      Read-only user directory
```

## Portfolio behavior

Every registered user receives a default portfolio with these simulated starting
balances:

```text
Indian Stock Market      ₹1,000,000
US Stock Exchange        $10,000
Crypto Currency Market  $10,000
```

The administrator receives the same wallet model plus the existing demo
holdings so the admin portfolio is populated on first bootstrap.

## Static files

Static assets were moved from `static/` to `public/static/` to match the current
Vercel Flask deployment guidance. Flask serves the same directory locally via a
small development route; Vercel serves `public/**` as static assets.
