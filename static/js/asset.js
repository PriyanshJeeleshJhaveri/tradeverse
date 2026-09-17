// TradeVerse asset detail page logic (single stock or crypto coin)
// Draws a live price chart (Chart.js) for the selected time range, and
// fills in current / low / high / change stats. Buy Now + Set Limit Order
// are intentionally not wired up to anything yet.

const ASSET_SYMBOL = window.TRADEVERSE_ASSET_SYMBOL;
const ASSET_TYPE = window.TRADEVERSE_ASSET_TYPE;

let chartInstance = null;

function formatMoney(value) {
    if (value === null || value === undefined || isNaN(value)) return "--";
    return "$" + Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 5 });
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
});
