// TradeVerse asset detail page logic (single stock or crypto coin)
// Draws a live price chart (Chart.js) for the selected time range, fills in
// current/low/high/change stats, and handles the Buy Now market-order modal.

const ASSET_SYMBOL = window.TRADEVERSE_ASSET_SYMBOL;
const ASSET_TYPE = window.TRADEVERSE_ASSET_TYPE;
const IS_INDIAN_STOCK = ASSET_TYPE === "stock" && (ASSET_SYMBOL.toUpperCase().endsWith(".NS") || ASSET_SYMBOL.toUpperCase().endsWith(".BO"));

let chartInstance = null;
let lastKnownPrice = null; // used only to show an estimated total in the buy modal

function formatMoney(value) {
    if (value === null || value === undefined || isNaN(value)) return "--";
    const prefix = IS_INDIAN_STOCK ? "₹" : "$";
    return prefix + Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: IS_INDIAN_STOCK ? 2 : 5 });
}

function setStatValue(elId, text, changeValue) {
    const el = document.getElementById(elId);
    el.textContent = text;
    if (changeValue !== undefined) {
        el.classList.remove("up", "down");
        el.classList.add(changeValue >= 0 ? "up" : "down");
    }
}

function renderChart(labels, prices, isUp) {
    const canvas = document.getElementById("priceChart");
    const lineColor = isUp ? "#1d8a3e" : "#c0392b";
    const fillColor = isUp ? "rgba(29, 138, 62, 0.12)" : "rgba(192, 57, 43, 0.12)";

    if (chartInstance) {
        chartInstance.destroy();
    }

    chartInstance = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
            labels: labels,
            datasets: [{
                data: prices,
                borderColor: lineColor,
                backgroundColor: fillColor,
                borderWidth: 2,
                fill: true,
                pointRadius: 0,
                pointHoverRadius: 4,
                tension: 0.15,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { maxTicksLimit: 6, color: "#7a8497", font: { size: 11 } }, grid: { display: false } },
                y: { ticks: { color: "#7a8497", font: { size: 11 } }, grid: { color: "#eef1f5" } },
            },
            interaction: { intersect: false, mode: "index" },
        },
    });
}

function loadChart(rangeKey) {
    document.querySelectorAll(".chart-tab").forEach(function (tab) {
        tab.classList.toggle("active", tab.getAttribute("data-range") === rangeKey);
    });

    const statusEl = document.getElementById("chartStatus");
    statusEl.textContent = "Loading chart...";
    statusEl.classList.remove("hidden");

    fetch("/api/asset/" + ASSET_TYPE + "/" + encodeURIComponent(ASSET_SYMBOL) + "/chart?range=" + rangeKey)
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (data.error) {
                statusEl.textContent = data.error;
                return;
            }
            statusEl.classList.add("hidden");

            const isUp = data.change_percent >= 0;
            renderChart(data.labels, data.prices, isUp);

            lastKnownPrice = data.current_price;

            setStatValue("statCurrent", formatMoney(data.current_price));
            setStatValue("statLow", formatMoney(data.period_low));
            setStatValue("statHigh", formatMoney(data.period_high));

            const pctSign = data.change_percent >= 0 ? "+" : "";
            setStatValue("statChangePercent", pctSign + data.change_percent.toFixed(2) + "%", data.change_percent);

            const valSign = data.change_value >= 0 ? "+" : "-";
            setStatValue("statChangeValue", valSign + formatMoney(Math.abs(data.change_value)), data.change_value);
        })
        .catch(function () {
            statusEl.textContent = "Could not load chart data. Please try again.";
            statusEl.classList.remove("hidden");
        });
}

// ---------------------------------------------------------------------------
// Buy Now modal - a market order at the current live price. The estimated
// total shown here uses the last price the chart fetched; the real trade is
// always priced server-side with a fresh quote at the moment of confirming.
// ---------------------------------------------------------------------------

function openBuyModal() {
    const qtyInput = document.getElementById("tradeQuantityInput");
    qtyInput.value = "1";
    qtyInput.step = ASSET_TYPE === "crypto" ? "any" : "1";

    document.getElementById("tradeModalSymbol").textContent = ASSET_SYMBOL;
    document.getElementById("tradeModalError").classList.add("hidden");
    document.getElementById("tradeModalSuccess").classList.add("hidden");
    document.getElementById("tradeModalConfirm").disabled = false;
    document.getElementById("tradeModalConfirm").textContent = "Confirm Buy";

    updateBuyModalTotal();
    document.getElementById("tradeModalOverlay").classList.remove("hidden");
}

function closeBuyModal() {
    document.getElementById("tradeModalOverlay").classList.add("hidden");
}

function readQuantity() {
    const raw = document.getElementById("tradeQuantityInput").value;
    return raw === "" ? NaN : parseFloat(raw);
}

function validateQuantity(quantity) {
    if (isNaN(quantity) || quantity <= 0) {
        return "Quantity must be greater than zero.";
    }
    if (ASSET_TYPE === "stock" && quantity !== Math.floor(quantity)) {
        return "Stock quantity must be a whole number of shares.";
    }
    return null;
}

function updateBuyModalTotal() {
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
    totalEl.textContent = lastKnownPrice !== null ? formatMoney(lastKnownPrice * quantity) : "--";
}

function submitBuy() {
    const quantity = readQuantity();
    const validationError = validateQuantity(quantity);
    if (validationError) {
        updateBuyModalTotal();
        return;
    }

    const confirmBtn = document.getElementById("tradeModalConfirm");
    const errorEl = document.getElementById("tradeModalError");
    const successEl = document.getElementById("tradeModalSuccess");

    confirmBtn.disabled = true;
    confirmBtn.textContent = "Placing order...";
    errorEl.classList.add("hidden");

    fetch("/api/trade/buy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: ASSET_SYMBOL, asset_type: ASSET_TYPE, quantity: quantity }),
    })
        .then(function (res) { return res.json().then(function (body) { return { ok: res.ok, body: body }; }); })
        .then(function (result) {
            if (!result.ok || result.body.error) {
                errorEl.textContent = result.body.error || "Something went wrong. Please try again.";
                errorEl.classList.remove("hidden");
                confirmBtn.disabled = false;
                confirmBtn.textContent = "Confirm Buy";
                return;
            }

            successEl.textContent = "Bought " + quantity + " " + ASSET_SYMBOL + " successfully!";
            successEl.classList.remove("hidden");
            confirmBtn.textContent = "Done";

            setTimeout(function () {
                closeBuyModal();
                loadChart(document.querySelector(".chart-tab.active").getAttribute("data-range"));
            }, 900);
        })
        .catch(function () {
            errorEl.textContent = "Could not reach the server. Please try again.";
            errorEl.classList.remove("hidden");
            confirmBtn.disabled = false;
            confirmBtn.textContent = "Confirm Buy";
        });
}

document.addEventListener("DOMContentLoaded", function () {
    // Hamburger dropdown toggle (shared with dashboard/market pages)
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

    // Time range tabs
    document.querySelectorAll(".chart-tab").forEach(function (tab) {
        tab.addEventListener("click", function () {
            loadChart(tab.getAttribute("data-range"));
        });
    });

    // Buy Now + modal wiring
    const buyBtn = document.getElementById("buyNowBtn");
    buyBtn.addEventListener("click", openBuyModal);

    document.getElementById("tradeQuantityInput").addEventListener("input", updateBuyModalTotal);
    document.getElementById("tradeModalConfirm").addEventListener("click", submitBuy);
    document.getElementById("tradeModalCancel").addEventListener("click", closeBuyModal);

    document.getElementById("tradeModalOverlay").addEventListener("click", function (e) {
        if (e.target === this) closeBuyModal();
    });

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeBuyModal();
    });

    loadChart("24H");
});
