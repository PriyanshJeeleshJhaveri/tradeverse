from __future__ import annotations

import os
import threading
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for, send_from_directory

load_dotenv()

import auth_db
import mongo_db
import portfolio_db
import price_service

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_STATIC_DIR = BASE_DIR / "public" / "static"

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()
if not FLASK_SECRET_KEY:
    if os.getenv("VERCEL"):
        raise RuntimeError("FLASK_SECRET_KEY is required in Vercel Environment Variables.")
    FLASK_SECRET_KEY = "tradeverse-local-development-secret-change-me"

app = Flask(__name__, static_folder=None)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
    MAX_CONTENT_LENGTH=256 * 1024,
    # Stay signed in for a week instead of logging out every time the browser closes.
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

MARKET_FULL_NAMES = {
    "ISE": "Indian Stock Market",
    "USE": "US Stock Exchange",
    "CCME": "Crypto Currency Market Exchange",
}

MARKET_CURRENCY_SYMBOL = {
    "ISE": "₹",
    "USE": "$",
    "CCME": "$",
}


def is_database_configured() -> bool:
    return bool(os.getenv("MONGODB_URI", "").strip())


def current_user():
    """The logged-in user, loaded from MongoDB at most once per request."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    if "current_user" not in g:
        g.current_user = auth_db.get_user_by_id(user_id)
    return g.current_user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        user = current_user()
        if user is None:
            # The account no longer exists (e.g. it was deleted) - end the session.
            session.clear()
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def api_login_required(view):
    """Same as login_required, but answers JSON 401 so the page's JavaScript
    can send the user back to the login page instead of choking on HTML."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id") or current_user() is None:
            session.clear()
            return jsonify({"error": "Please log in again."}), 401
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        if user.get("role") != "ADMIN":
            flash("You do not have permission to access the admin area.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_user():
    user = current_user() if session.get("user_id") else None
    return {
        "current_user": user,
        "is_admin": bool(user and user.get("role") == "ADMIN"),
    }


# ---------------------------------------------------------------------------
# Database bootstrap (indexes + admin account). Runs once per server instance;
# if MongoDB was unreachable at that moment it is retried (at most every 30 s)
# instead of being skipped until the next cold start.
# ---------------------------------------------------------------------------

_bootstrap = {"done": False, "last_try": 0.0}
_bootstrap_lock = threading.Lock()


def ensure_bootstrap() -> None:
    if _bootstrap["done"] or not is_database_configured():
        return
    if time.time() - _bootstrap["last_try"] < 30:
        return
    with _bootstrap_lock:
        if _bootstrap["done"]:
            return
        _bootstrap["last_try"] = time.time()
        try:
            auth_db.init_indexes()
            portfolio_db.init_indexes()
            price_service.init_cache_indexes()
            admin = auth_db.ensure_admin_user()
            if admin:
                portfolio_db.ensure_admin_portfolio(admin["id"], admin["username"])
            _bootstrap["done"] = True
        except Exception as exc:
            # Do not make the application unusable just because Atlas is
            # temporarily unavailable; routes surface a clear error instead.
            print(f"[startup] MongoDB initialization skipped (will retry): {exc}")


@app.before_request
def _bootstrap_before_request():
    if request.endpoint in ("local_static", "health"):
        return None
    ensure_bootstrap()
    return None


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Account pages and API answers must never be served from a browser cache
    # (e.g. pressing Back after logging out must not reveal a portfolio).
    if request.endpoint != "local_static":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/static/<path:filename>")
def local_static(filename):
    """Serve public/static locally; Vercel serves the same directory as CDN assets."""
    return send_from_directory(PUBLIC_STATIC_DIR, filename)


@app.route("/health")
def health():
    """Deployment health check without exposing database credentials."""
    if not is_database_configured():
        return jsonify({"status": "error", "database": "not_configured"}), 503
    try:
        mongo_db.ping()
        return jsonify({"status": "ok", "database": "ok"})
    except Exception as exc:
        print(f"[health] {exc}")
        return jsonify({"status": "error", "database": "unavailable"}), 503


@app.route("/")
def home():
    user = current_user()
    if user:
        if user.get("role") == "ADMIN":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        # Keep what the person typed (never the password) if we need to show the form again.
        form = {"username": username, "email": email, "phone": phone}

        if not all([username, email, phone, password]):
            flash("Please fill in all fields.", "error")
            return render_template("register.html", form=form)

        password_error = auth_db.validate_password(password)
        if password_error:
            flash(password_error, "error")
            return render_template("register.html", form=form)

        user = None
        try:
            if not is_database_configured():
                raise RuntimeError("MongoDB Atlas is not configured yet.")
            user = auth_db.create_user(username, email, phone, password, role="USER")
            portfolio_db.create_default_portfolio(user["id"], user["username"], role="USER")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("register.html", form=form)
        except Exception as exc:
            print(f"[register] {exc}")
            if user is not None:
                # The account was created but its portfolio was not: undo it so
                # the person can simply try again with the same details.
                try:
                    auth_db.delete_user(user["id"])
                except Exception as cleanup_exc:
                    print(f"[register] cleanup failed: {cleanup_exc}")
            flash("The account could not be created right now. Please try again in a moment.", "error")
            return render_template("register.html", form=form)

        flash("Account created successfully! You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        try:
            if not is_database_configured():
                raise RuntimeError("MongoDB Atlas is not configured yet.")
            user = auth_db.verify_credentials(username, password)
        except Exception as exc:
            print(f"[login] {exc}")
            flash("Database connection is not ready yet. Please check the MongoDB/Vercel configuration.", "error")
            return render_template("login.html")

        if user:
            # Self-heal: an account must always have a portfolio/wallet.
            if user.get("role") != "ADMIN":
                try:
                    portfolio_db.ensure_portfolio_exists(user["id"], user["username"], role="USER")
                except Exception as exc:
                    print(f"[login] could not ensure portfolio: {exc}")
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user.get("role", "USER")
            flash("Welcome back, " + user["username"] + "!", "success")
            if user.get("role") == "ADMIN":
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dashboard"))

        flash("Invalid username or password.", "error")
        return render_template("login.html")

    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        flash("Password reset email delivery is not configured yet. Please contact the administrator.", "error")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    return render_template("dashboard.html", username=user["username"])


@app.route("/market/<market>")
@login_required
def market_page(market):
    market = market.upper()
    if market not in portfolio_db.MARKETS:
        flash("Unknown market.", "error")
        return redirect(url_for("dashboard"))

    user = current_user()
    return render_template(
        "market.html",
        username=user["username"],
        market=market,
        market_name=MARKET_FULL_NAMES[market],
        currency_symbol=MARKET_CURRENCY_SYMBOL[market],
    )


@app.route("/transactions")
@login_required
def transaction_history():
    user = current_user()
    all_transactions = portfolio_db.get_transaction_history(user["id"])
    transactions = {
        market: [t for t in all_transactions if t.get("market") == market]
        for market in portfolio_db.MARKETS
    }
    return render_template(
        "transactions.html",
        username=user["username"],
        transactions=transactions,
        market_names=MARKET_FULL_NAMES,
        currency_symbols=MARKET_CURRENCY_SYMBOL,
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Admin dashboard and portfolio management
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    user = current_user()
    portfolio = portfolio_db.get_portfolio_doc(user["id"])
    if portfolio is None:
        portfolio = portfolio_db.ensure_admin_portfolio(user["id"], user["username"])

    holdings_count = sum(len(portfolio.get(market + "_portfolio", [])) for market in portfolio_db.MARKETS)
    return render_template(
        "admin/dashboard.html",
        admin=user,
        portfolio=portfolio,
        holdings_count=holdings_count,
        user_count=auth_db.count_users(),
    )


@app.route("/admin/portfolio", methods=["GET", "POST"])
@admin_required
def admin_portfolio():
    user = current_user()
    portfolio = portfolio_db.get_portfolio_doc(user["id"])
    if portfolio is None:
        portfolio = portfolio_db.ensure_admin_portfolio(user["id"], user["username"])

    if request.method == "POST":
        try:
            portfolio_db.update_portfolio_metadata(
                user["id"],
                request.form.get("portfolio_name", ""),
                request.form.get("description", ""),
            )
            flash("Admin portfolio details saved.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_portfolio"))

    return render_template("admin/portfolio.html", admin=user, portfolio=portfolio)


@app.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin/users.html", users=auth_db.list_users())


# ---------------------------------------------------------------------------
# JSON APIs used by the dashboard's JavaScript
# ---------------------------------------------------------------------------

@app.route("/api/portfolio/<market>")
@api_login_required
def api_portfolio(market):
    market = market.upper()
    if market not in portfolio_db.MARKETS:
        return jsonify({"error": "invalid market"}), 400

    try:
        snapshot = portfolio_db.get_market_snapshot(session["user_id"], market)
    except Exception as exc:
        print(f"[api_portfolio] {exc}")
        return jsonify({"error": "Could not load your portfolio right now. Please try again."}), 503
    snapshot["market"] = market
    return jsonify(snapshot)


@app.route("/api/btc")
@api_login_required
def api_btc():
    quote = price_service.get_quote("BTC-USD")
    if quote is None:
        return jsonify({"price": None, "change": None, "change_percent": None})
    return jsonify(quote)


@app.route("/api/indices")
@api_login_required
def api_indices():
    # Live from Yahoo Finance; falls back to placeholder numbers (flagged with
    # "proxy": true) only if Yahoo cannot be reached at all.
    return jsonify(price_service.get_index_quotes())


@app.route("/api/search")
@api_login_required
def api_search():
    query = request.args.get("q", "").strip()[:40]
    if len(query) < 1:
        return jsonify([])
    return jsonify(price_service.search_symbols(query))


# ---------------------------------------------------------------------------
# Asset detail page
# ---------------------------------------------------------------------------

@app.route("/asset/<asset_type>/<symbol>")
@login_required
def asset_page(asset_type, symbol):
    asset_type = asset_type.lower()
    if asset_type not in ("stock", "crypto"):
        flash("Unknown asset type.", "error")
        return redirect(url_for("dashboard"))

    user = current_user()
    symbol = symbol.strip().upper()
    if not price_service.is_valid_symbol(symbol):
        flash("That symbol is not valid.", "error")
        return redirect(url_for("dashboard"))
    display_name = request.args.get("name", "").strip()[:80] or symbol

    return render_template(
        "asset.html",
        username=user["username"],
        symbol=symbol,
        asset_type=asset_type,
        display_name=display_name,
    )


@app.route("/api/asset/<asset_type>/<symbol>/chart")
@api_login_required
def api_asset_chart(asset_type, symbol):
    asset_type = asset_type.lower()
    if asset_type not in ("stock", "crypto"):
        return jsonify({"error": "invalid asset type"}), 400

    range_key = request.args.get("range", "24H").upper()
    if range_key not in price_service.CHART_RANGES:
        return jsonify({"error": "invalid range"}), 400

    if not price_service.is_valid_symbol(symbol):
        return jsonify({"error": "invalid symbol"}), 400

    data = price_service.get_chart_data(symbol.upper(), asset_type, range_key)
    if data is None:
        return jsonify({"error": "Could not load chart data for this symbol right now."}), 502

    return jsonify(data)


# ---------------------------------------------------------------------------
# Buy / Sell APIs
# ---------------------------------------------------------------------------

@app.route("/api/trade/buy", methods=["POST"])
@api_login_required
def api_trade_buy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    symbol = str(data.get("symbol") or "").strip()
    asset_type = str(data.get("asset_type") or "").strip().lower()
    quantity = data.get("quantity")

    if not symbol or asset_type not in ("stock", "crypto"):
        return jsonify({"error": "Invalid request."}), 400

    try:
        ok, result = portfolio_db.buy_asset(session["user_id"], symbol, asset_type, quantity)
    except Exception as exc:
        print(f"[api_trade_buy] {exc}")
        return jsonify({"error": "Could not place the order right now. Please try again."}), 503
    if not ok:
        return jsonify({"error": result}), 400

    return jsonify({"success": True, **result})


@app.route("/api/trade/sell", methods=["POST"])
@api_login_required
def api_trade_sell():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    market = str(data.get("market") or "").strip()
    lot_id = str(data.get("lot_id") or "").strip()
    quantity = data.get("quantity")

    if not lot_id or not market:
        return jsonify({"error": "Invalid request."}), 400

    try:
        ok, result = portfolio_db.sell_lot(session["user_id"], market, lot_id, quantity)
    except Exception as exc:
        print(f"[api_trade_sell] {exc}")
        return jsonify({"error": "Could not place the order right now. Please try again."}), 503
    if not ok:
        return jsonify({"error": result}), 400

    return jsonify({"success": True, **result})


# ---------------------------------------------------------------------------
# Startup bootstrap (also retried automatically by before_request if it fails)
# ---------------------------------------------------------------------------

ensure_bootstrap()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "1") == "1")
