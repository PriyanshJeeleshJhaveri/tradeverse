// TradeVerse dashboard (landing page) logic

const REFRESH_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes, matches backend price cache
const SEARCH_DEBOUNCE_MS = 400; // a little slower = fewer API calls on the free plans

let searchDebounceTimer = null;
let searchRequestSeq = 0; // guards against a slow older request overwriting a newer one


function loadBtcHero() {
    apiFetch("/api/btc")
        .then((res) => res.json())
        .then((data) => {
            const priceEl = document.getElementById("btcPrice");
            const changeEl = document.getElementById("btcChange");
            applyQuoteToElements(data, priceEl, changeEl, "$");
        })
        .catch(() => {
            document.getElementById("btcPrice").textContent = "Unavailable";
        });
}

// `prefix` is the currency symbol: "$" for Bitcoin, "" for the stock indices
// (index values are points, not dollars).
function applyQuoteToElements(quote, priceEl, changeEl, prefix) {
    if (!quote || quote.price === null || quote.price === undefined) {
        priceEl.textContent = "Unavailable";
        changeEl.textContent = "";
        return;
    }

    priceEl.textContent = (prefix || "") + Number(quote.price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    if (quote.change !== null && quote.change !== undefined) {
        const sign = quote.change >= 0 ? "+" : "";
        const pct = (quote.change_percent !== null && quote.change_percent !== undefined)
            ? " (" + sign + Number(quote.change_percent).toFixed(2) + "%)"
            : "";
        changeEl.textContent = sign + Number(quote.change).toFixed(2) + pct + (quote.proxy ? " · est." : "");
        changeEl.className = "idx-change " + (quote.change >= 0 ? "up" : "down");
    } else {
        changeEl.textContent = "";
    }
}

// Maps the /api/indices response (S&P 500 / NIFTY 50 / SENSEX) onto the
// index boxes already in the page.
function loadIndices() {
    const boxIdByName = {
        "S&P 500": "idx-sp500",
        "NIFTY 50": "idx-nifty50",
        "SENSEX": "idx-sensex",
    };

    apiFetch("/api/indices")
        .then((res) => res.json())
        .then((data) => {
            if (!Array.isArray(data)) return;
            data.forEach(function (idx) {
                const boxId = boxIdByName[idx.name];
                const box = boxId ? document.getElementById(boxId) : null;
                if (!box) return;

                const priceEl = box.querySelector(".idx-price");
                const changeEl = box.querySelector(".idx-change");
                applyQuoteToElements(idx, priceEl, changeEl, "");
            });
        })
        .catch(() => {});
}


// ---------------------------------------------------------------------------
// Live search - fires /api/search as the user types, debounced, and shows
// a dropdown of matching stocks/crypto under the search bar. Clicking a
// result takes the user to that asset's detail page.
// ---------------------------------------------------------------------------

function renderSearchResults(results, query) {
    const dropdown = document.getElementById("searchResultsDropdown");

    if (!Array.isArray(results) || results.length === 0) {
        dropdown.innerHTML = '<div class="search-dropdown-empty">No matches for "' + escapeHtml(query) + '"</div>';
        dropdown.classList.remove("hidden");
        return;
    }

    dropdown.innerHTML = results.map(function (r) {
        const type = r.type === "crypto" ? "crypto" : "stock";
        const sub = type === "stock" ? (r.exchange || "Stock") : "Cryptocurrency";
        return (
            '<div class="search-dropdown-item" data-symbol="' + escapeHtml(r.symbol) + '" data-type="' + type + '" data-name="' + escapeHtml(r.name) + '">' +
                '<div>' +
                    '<div class="sdi-name">' + escapeHtml(r.symbol) + ' &middot; ' + escapeHtml(r.name) + '</div>' +
                    '<div class="sdi-sub">' + escapeHtml(sub) + '</div>' +
                '</div>' +
                '<span class="sdi-type-badge ' + type + '">' + type + '</span>' +
            '</div>'
        );
    }).join("");

    dropdown.classList.remove("hidden");

    dropdown.querySelectorAll(".search-dropdown-item").forEach(function (item) {
        item.addEventListener("click", function () {
            const symbol = item.getAttribute("data-symbol");
            const type = item.getAttribute("data-type");
            const name = item.getAttribute("data-name");
            window.location.href = "/asset/" + type + "/" + encodeURIComponent(symbol) + "?name=" + encodeURIComponent(name);
        });
    });
}

function runSearch(query) {
    const dropdown = document.getElementById("searchResultsDropdown");

    if (!query) {
        dropdown.classList.add("hidden");
        dropdown.innerHTML = "";
        return;
    }

    dropdown.innerHTML = '<div class="search-dropdown-loading">Searching...</div>';
    dropdown.classList.remove("hidden");

    const seq = ++searchRequestSeq;
    apiFetch("/api/search?q=" + encodeURIComponent(query))
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (seq !== searchRequestSeq) return; // a newer keystroke already fired, drop this stale response
            renderSearchResults(data, query);
        })
        .catch(function () {
            if (seq !== searchRequestSeq) return;
            dropdown.innerHTML = '<div class="search-dropdown-empty">Search unavailable right now. Please try again.</div>';
        });
}

document.addEventListener("DOMContentLoaded", function () {
    // Hamburger dropdown toggle
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

    // Live search wiring
    const searchInput = document.getElementById("searchInput");
    const searchBtn = document.getElementById("searchBtn");
    const searchDropdown = document.getElementById("searchResultsDropdown");

    searchInput.addEventListener("input", function () {
        const query = searchInput.value.trim();
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(function () {
            runSearch(query);
        }, SEARCH_DEBOUNCE_MS);
    });

    searchInput.addEventListener("focus", function () {
        if (searchInput.value.trim() && searchDropdown.innerHTML.trim()) {
            searchDropdown.classList.remove("hidden");
        }
    });

    searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
            searchDropdown.classList.add("hidden");
        } else if (e.key === "Enter") {
            clearTimeout(searchDebounceTimer);
            runSearch(searchInput.value.trim());
        }
    });

    searchBtn.addEventListener("click", function () {
        clearTimeout(searchDebounceTimer);
        runSearch(searchInput.value.trim());
    });

    document.addEventListener("click", function (e) {
        if (!searchDropdown.contains(e.target) && e.target !== searchInput && e.target !== searchBtn) {
            searchDropdown.classList.add("hidden");
        }
    });

    // Initial load
    loadBtcHero();
    loadIndices();

    // Auto-refresh every 10 minutes to line up with the backend price cache.
    // Skipped while the tab is hidden - saves server calls on the free plan.
    setInterval(function () {
        if (document.hidden) return;
        loadBtcHero();
        loadIndices();
    }, REFRESH_INTERVAL_MS);
});
