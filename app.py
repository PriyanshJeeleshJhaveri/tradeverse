from __future__ import annotations

import os
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for, send_from_directory
from werkzeug.security import check_password_hash

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
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
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
    user_id = session.get("user_id")
    if not user_id:
        return None
    return auth_db.get_user_by_id(user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
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

        if not all([username, email, phone, password]):
            flash("Please fill in all fields.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "error")
            return render_template("register.html")

        try:
            if not is_database_configured():
                raise RuntimeError("MongoDB Atlas is not configured yet.")
            user = auth_db.create_user(username, email, phone, password, role="USER")
            portfolio_db.create_default_portfolio(user["id"], user["username"], role="USER")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("register.html")
        except Exception as exc:
            print(f"[register] {exc}")
            flash("The account could not be created right now. Check the database configuration and try again.", "error")
            return render_template("register.html")

        flash("Account created successfully! You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


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
            session.clear()
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
@login_required
def api_portfolio(market):
    market = market.upper()
    if market not in portfolio_db.MARKETS:
        return jsonify({"error": "invalid market"}), 400

    snapshot = portfolio_db.get_market_snapshot(session["user_id"], market)
    snapshot["market"] = market
    return jsonify(snapshot)


@app.route("/api/btc")
@login_required
def api_btc():
    quote = price_service.get_quote("BTC-USD")
    if quote is None:
        return jsonify({"price": None, "change": None, "change_percent": None})
    return jsonify(quote)


@app.route("/api/indices")
@login_required
def api_indices():
    return jsonify([
        {"symbol": "SPX", "name": "S&P 500", "price": 6250.00, "change": 28.13, "change_percent": 0.45},
        {"symbol": "NIFTY", "name": "NIFTY 50", "price": 25000.00, "change": 95.00, "change_percent": 0.38},
        {"symbol": "SENSEX", "name": "SENSEX", "price": 82000.00, "change": 336.20, "change_percent": 0.41},
    ])


@app.route("/api/search")
@login_required
def api_search():
    query = request.args.get("q", "").strip()
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
    symbol = symbol.upper()
    display_name = request.args.get("name", "").strip() or symbol

    return render_template(
        "asset.html",
        username=user["username"],
        symbol=symbol,
        asset_type=asset_type,
        display_name=display_name,
    )


@app.route("/api/asset/<asset_type>/<symbol>/chart")
@login_required
def api_asset_chart(asset_type, symbol):
    asset_type = asset_type.lower()
    if asset_type not in ("stock", "crypto"):
        return jsonify({"error": "invalid asset type"}), 400

    range_key = request.args.get("range", "24H").upper()
    if range_key not in price_service.CHART_RANGES:
        return jsonify({"error": "invalid range"}), 400

    data = price_service.get_chart_data(symbol.upper(), asset_type, range_key)
    if data is None:
        return jsonify({"error": "Could not load chart data for this symbol right now."}), 502

    return jsonify(data)


# ---------------------------------------------------------------------------
# Buy / Sell APIs
# ---------------------------------------------------------------------------

@app.route("/api/trade/buy", methods=["POST"])
@login_required
def api_trade_buy():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip()
    asset_type = (data.get("asset_type") or "").strip().lower()
    quantity = data.get("quantity")

    if not symbol or asset_type not in ("stock", "crypto"):
        return jsonify({"error": "Invalid request."}), 400

    ok, result = portfolio_db.buy_asset(session["user_id"], symbol, asset_type, quantity)
    if not ok:
        return jsonify({"error": result}), 400

    return jsonify({"success": True, **result})


@app.route("/api/trade/sell", methods=["POST"])
@login_required
def api_trade_sell():
    data = request.get_json(silent=True) or {}
    market = (data.get("market") or "").strip()
    lot_id = (data.get("lot_id") or "").strip()
    quantity = data.get("quantity")

    if not lot_id or not market:
        return jsonify({"error": "Invalid request."}), 400

    ok, result = portfolio_db.sell_lot(session["user_id"], market, lot_id, quantity)
    if not ok:
        return jsonify({"error": result}), 400

    return jsonify({"success": True, **result})


# ---------------------------------------------------------------------------
# Startup bootstrap
# ---------------------------------------------------------------------------

try:
    if is_database_configured():
        auth_db.init_indexes()
        portfolio_db.init_indexes()
        admin = auth_db.ensure_admin_user()
        if admin:
            portfolio_db.ensure_admin_portfolio(admin["id"], admin["username"])
except Exception as exc:
    # Do not make the application completely unimportable just because Atlas
    # is temporarily unavailable. Auth/database routes will surface a useful
    # configuration error instead.
    print(f"[startup] MongoDB initialization skipped: {exc}")


if __name__ == "__main__":
    app.run(debug=True)
