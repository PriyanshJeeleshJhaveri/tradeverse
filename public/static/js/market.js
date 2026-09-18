// TradeVerse market page logic (Indian Stock Market / US Stock Exchange / Crypto)

const REFRESH_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes, matches backend price cache
const MARKET = window.TRADEVERSE_MARKET;
const CURRENCY_SYMBOL = window.TRADEVERSE_CURRENCY_SYMBOL;
const COLUMN_COUNT = 9;
const IS_CRYPTO_MARKET = MARKET === "CCME";
const SELLING_SUPPORTED = true;

function formatMoney(value) {
    if (value === null || value === undefined || isNaN(value)) return "--";
    const num = Number(value);
    return CURRENCY_SYMBOL + num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatQuantity(value) {
    if (value === null || value === undefined || isNaN(value)) return "--";
    const num = Number(value);
    // Crypto holdings can be fractional (e.g. 0.1 BTC) - show up to 4 decimals, but
    // don't pad whole-share stock quantities with trailing zeros.
    return Number.isInteger(num) ? String(num) : num.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function formatDate(isoDateStr) {
    if (!isoDateStr) return "--";
    const d = new Date(isoDateStr + "T00:00:00Z");
    if (isNaN(d.getTime())) return isoDateStr;
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

function setLoadingRow(message) {
    document.getElementById("portfolioBody").innerHTML =
        '<tr><td colspan="' + COLUMN_COUNT + '" class="loading-row">' + message + "</td></tr>";
}

function renderHoldingRow(h) {
    const plUp = h.profit_loss >= 0;
    const plSign = plUp ? "+" : "";
    const plClass = plUp ? "up" : "down";

    const sellNowBtn = SELLING_SUPPORTED
        ? "<button type=\"button\" class=\"btn-sell-now\" data-lot-id=\"" + h.id + "\" data-symbol=\"" + h.name +
          "\" data-price=\"" + h.price + "\" data-available=\"" + h.quantity + "\">Sell Now</button>"
        : "<button type=\"button\" class=\"btn-sell-now\" disabled title=\"Indian stock market trading isn't available yet.\">Sell Now</button>";

    const sellLimitBtn = "<button type=\"button\" class=\"btn-sell-limit\"" +
        (SELLING_SUPPORTED ? " title=\"Coming soon\"" : " disabled title=\"Indian stock market trading isn't available yet.\"") +
        ">Sell Limit</button>";

    return (
        "<tr>" +
            "<td>" + h.name + "</td>" +
            "<td>" + formatDate(h.bought_date) + "</td>" +
            "<td class=\"num\">" + formatMoney(h.bought_price) + "</td>" +
            "<td class=\"num\">" + formatMoney(h.price) + "</td>" +
            "<td class=\"num\">" + formatQuantity(h.quantity) + "</td>" +
            "<td class=\"num\">" + formatMoney(h.total_amount) + "</td>" +
            "<td class=\"num pl-cell " + plClass + "\">" + plSign + formatMoney(h.profit_loss) + "</td>" +
            "<td class=\"num pl-cell " + plClass + "\">" + plSign + h.profit_loss_percent.toFixed(2) + "%</td>" +
            "<td class=\"actions-cell\">" + sellNowBtn + sellLimitBtn + "</td>" +
        "</tr>"
    );
}

function loadPortfolio() {
    setLoadingRow("Loading portfolio...");

    fetch("/api/portfolio/" + MARKET)
        .then((res) => res.json())
        .then((data) => {
            if (data.error) {
                setLoadingRow(data.error);
                return;
            }

            document.getElementById("walletAmount").textContent = formatMoney(data.wallet);

            if (!data.holdings || data.holdings.length === 0) {
                setLoadingRow("No holdings yet in this portfolio.");
                return;
            }

            document.getElementById("portfolioBody").innerHTML = data.holdings.map(renderHoldingRow).join("");

            document.querySelectorAll(".btn-sell-now:not([disabled])").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    openSellModal({
                        lotId: btn.getAttribute("data-lot-id"),
                        symbol: btn.getAttribute("data-symbol"),
                        price: parseFloat(btn.getAttribute("data-price")),
                        available: parseFloat(btn.getAttribute("data-available")),
                    });
                });
            });
        })
        .catch(() => {
            setLoadingRow("Could not load portfolio. Please try again.");
        });
}


// ---------------------------------------------------------------------------
// Sell Now modal - a market order at the current live price, capped at the
// quantity remaining in that specific lot.
// ---------------------------------------------------------------------------

let activeLot = null;

function openSellModal(lot) {
    activeLot = lot;

    const qtyInput = document.getElementById("tradeQuantityInput");
    qtyInput.value = "1";
    qtyInput.step = IS_CRYPTO_MARKET ? "any" : "1";
    qtyInput.max = lot.available;

    document.getElementById("tradeModalSymbol").textContent = lot.symbol;
    document.getElementById("tradeModalAvailable").textContent = "(you own " + formatQuantity(lot.available) + ")";
    document.getElementById("tradeModalError").classList.add("hidden");
    document.getElementById("tradeModalSuccess").classList.add("hidden");
    document.getElementById("tradeModalConfirm").disabled = false;
    document.getElementById("tradeModalConfirm").textContent = "Confirm Sell";

    updateSellModalTotal();
    document.getElementById("tradeModalOverlay").classList.remove("hidden");
}

function closeSellModal() {
    document.getElementById("tradeModalOverlay").classList.add("hidden");
    activeLot = null;
}

function readQuantity() {
    const raw = document.getElementById("tradeQuantityInput").value;
    return raw === "" ? NaN : parseFloat(raw);
}

function validateQuantity(quantity) {
    if (!activeLot) return "No holding selected.";
    if (isNaN(quantity) || quantity <= 0) {
        return "Quantity must be greater than zero.";
    }
    if (!IS_CRYPTO_MARKET && quantity !== Math.floor(quantity)) {
        return "Stock quantity must be a whole number of shares.";
    }
    if (quantity > activeLot.available + 1e-9) {
        return "You can only sell up to " + formatQuantity(activeLot.available) + " from this lot.";
    }
    return null;
}

function updateSellModalTotal() {
    const quantity = readQuantity();
    const errorEl = document.getElementById("tradeModalError");
    const confirmBtn = document.getElementById("tradeModalConfirm");
    const totalEl = document.getElementById("tradeModalTotal");

    const validationError = validateQuantity(quantity);
    if (validationError) {
        totalEl.textContent = "--";
        errorEl.textContent = validationError;
        errorEl.classList.remove("hidden");
        confirmBtn.disabled = true;
        return;
    }

    errorEl.classList.add("hidden");
    confirmBtn.disabled = false;
    totalEl.textContent = formatMoney(activeLot.price * quantity);
}

function submitSell() {
    const quantity = readQuantity();
    const validationError = validateQuantity(quantity);
    if (validationError) {
        updateSellModalTotal();
        return;
    }

    const confirmBtn = document.getElementById("tradeModalConfirm");
    const errorEl = document.getElementById("tradeModalError");
    const successEl = document.getElementById("tradeModalSuccess");

    confirmBtn.disabled = true;
    confirmBtn.textContent = "Placing order...";
    errorEl.classList.add("hidden");

    fetch("/api/trade/sell", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market: MARKET, lot_id: activeLot.lotId, quantity: quantity }),
    })
        .then(function (res) { return res.json().then(function (body) { return { ok: res.ok, body: body }; }); })
        .then(function (result) {
            if (!result.ok || result.body.error) {
                errorEl.textContent = result.body.error || "Something went wrong. Please try again.";
                errorEl.classList.remove("hidden");
                confirmBtn.disabled = false;
                confirmBtn.textContent = "Confirm Sell";
                return;
            }

            successEl.textContent = "Sold " + quantity + " " + activeLot.symbol + " successfully!";
            successEl.classList.remove("hidden");
            confirmBtn.textContent = "Done";

            setTimeout(function () {
                closeSellModal();
                loadPortfolio();
            }, 900);
        })
        .catch(function () {
            errorEl.textContent = "Could not reach the server. Please try again.";
            errorEl.classList.remove("hidden");
            confirmBtn.disabled = false;
            confirmBtn.textContent = "Confirm Sell";
        });
}

document.addEventListener("DOMContentLoaded", function () {
    // Hamburger dropdown toggle (links navigate normally, so we only need open/close)
    const menuBtn = document.getElementById("menuBtn");
    const dropdownMenu = document.getElementById("dropdownMenu");

    menuBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        dropdownMenu.classList.toggle("hidden");
    });

    document.addEventListener("click", function (e) {
        if (!dropdownMenu.contains(e.target) && e.target !== menuBtn) {
            dropdownMenu.classList.add("hidden");
        }
    });

    document.getElementById("tradeQuantityInput").addEventListener("input", updateSellModalTotal);
    document.getElementById("tradeModalConfirm").addEventListener("click", submitSell);
    document.getElementById("tradeModalCancel").addEventListener("click", closeSellModal);

    document.getElementById("tradeModalOverlay").addEventListener("click", function (e) {
        if (e.target === this) closeSellModal();
    });

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeSellModal();
    });

    loadPortfolio();

    setInterval(loadPortfolio, REFRESH_INTERVAL_MS);
});
