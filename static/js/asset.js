// TradeVerse asset detail page logic (single stock or crypto coin)
// Draws a live price chart (Chart.js) for the selected time range, fills in
// price stats, and wires the supported immediate Buy action.

const ASSET_SYMBOL = window.TRADEVERSE_ASSET_SYMBOL;
const ASSET_TYPE = window.TRADEVERSE_ASSET_TYPE;

let chartInstance = null;
let currentPrice = null;
const CURRENCY_SYMBOL = "$";

function formatMoney(value) {
    if (value === null || value === undefined || isNaN(value)) return "--";
    return CURRENCY_SYMBOL + Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 5 });
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
            currentPrice = Number(data.current_price);
            renderChart(data.labels, data.prices, isUp);

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


function isIndianStock() {
    return ASSET_TYPE === "stock" && (ASSET_SYMBOL.endsWith(".NS") || ASSET_SYMBOL.endsWith(".BO"));
}

function openBuyModal() {
    if (isIndianStock()) {
        alert("Trading for Indian stocks is not available yet.");
        return;
    }
    if (!Number.isFinite(currentPrice) || currentPrice <= 0) {
        alert("Current price is not available. Please wait for the price to load and try again.");
        return;
    }

    const market = ASSET_TYPE === "crypto" ? "CCME" : "USE";
    fetch("/api/portfolio/" + market)
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (data.error) {
                alert(data.error);
                return;
            }
            TradeModal.open({
                action: "buy",
                assetType: ASSET_TYPE,
                market: market,
                symbol: ASSET_SYMBOL,
                currentPrice: currentPrice,
                currencySymbol: "$",
                balance: Number(data.wallet),
                onSuccess: function (result) {
                    alert("Buy completed successfully. New wallet balance: $" + Number(result.wallet).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
                }
            });
        })
        .catch(function () {
            alert("Could not load your wallet balance. Please try again.");
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

    loadChart("24H");

    const buyButton = document.querySelector(".btn-buy");
    if (buyButton) buyButton.addEventListener("click", openBuyModal);

    const limitButton = document.querySelector(".btn-limit");
    if (limitButton) limitButton.addEventListener("click", function () {
        alert("Limit orders are not available yet. Use Buy Now for an immediate paper trade.");
    });
});
