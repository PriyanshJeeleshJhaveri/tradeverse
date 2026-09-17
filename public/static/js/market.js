// TradeVerse market page logic (Indian Stock Market / US Stock Exchange / Crypto)

const REFRESH_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes, matches backend price cache
const MARKET = window.TRADEVERSE_MARKET;
const CURRENCY_SYMBOL = window.TRADEVERSE_CURRENCY_SYMBOL;
const COLUMN_COUNT = 9;

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
            "<td class=\"actions-cell\">" +
                (MARKET === "ISE" ? "<span class=\"trade-disabled\">Trading unavailable</span>" :
                    "<button type=\"button\" class=\"btn-sell-now\" data-action=\"sell\" data-symbol=\"" + h.name + "\" data-lot-id=\"" + (h.lot_id || "") + "\" data-price=\"" + h.price + "\" data-quantity=\"" + h.quantity + "\">Sell Now</button>" +
                    "<button type=\"button\" class=\"btn-sell-limit\" data-action=\"sell-limit\" data-symbol=\"" + h.name + "\">Sell Limit</button>") +
            "</td>" +
        "</tr>"
    );
}


function bindTradeButtons(data) {
    document.querySelectorAll("[data-action='sell']").forEach(function (button) {
        button.addEventListener("click", function () {
            const maxQuantity = Number(button.dataset.quantity);
            const price = Number(button.dataset.price);
            if (!button.dataset.lotId) {
                loadPortfolio();
                return;
            }
            TradeModal.open({
                action: "sell",
                market: MARKET,
                assetType: MARKET === "CCME" ? "crypto" : "stock",
                symbol: button.dataset.symbol,
                lotId: button.dataset.lotId,
                maxQuantity: maxQuantity,
                currentPrice: price,
                currencySymbol: CURRENCY_SYMBOL,
                onSuccess: function () { loadPortfolio(); }
            });
        });
    });

    document.querySelectorAll("[data-action='sell-limit']").forEach(function (button) {
        button.addEventListener("click", function () {
            alert("Limit orders are not available yet. Use Sell Now for an immediate paper trade.");
        });
    });
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
            bindTradeButtons(data);
        })
        .catch(() => {
            setLoadingRow("Could not load portfolio. Please try again.");
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

    loadPortfolio();

    setInterval(loadPortfolio, REFRESH_INTERVAL_MS);
});
