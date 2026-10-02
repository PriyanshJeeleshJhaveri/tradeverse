// TradeVerse - simple front-end helper script

// fetch() wrapper for the JSON APIs: if the login session has expired the
// server answers 401, and we send the person back to the login page instead
// of failing with a confusing "could not load" message.
function apiFetch(url, options) {
    return fetch(url, options).then(function (res) {
        if (res.status === 401) {
            window.location.href = "/login";
            return new Promise(function () {}); // page is navigating away
        }
        return res;
    });
}

// Escape text before putting it into innerHTML (names/symbols come from
// third-party APIs, so never trust them as HTML).
function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function togglePassword(inputId, toggleEl) {
    var input = document.getElementById(inputId);
    if (input.type === "password") {
        input.type = "text";
        toggleEl.textContent = "HIDE";
    } else {
        input.type = "password";
        toggleEl.textContent = "SHOW";
    }
}

// Auto-hide flash messages after a few seconds
window.addEventListener("load", function () {
    var flashes = document.querySelectorAll(".flash");
    flashes.forEach(function (el) {
        setTimeout(function () {
            el.style.transition = "opacity 0.5s ease";
            el.style.opacity = "0";
            setTimeout(function () {
                el.style.display = "none";
            }, 500);
        }, 4000);
    });
});

// Basic client side validation for register form
function validateRegisterForm() {
    var phoneDigits = document.getElementById("phone").value.replace(/\D/g, "");
    var password = document.getElementById("password").value;

    // Same rules as the server (10-15 digits, 8+ character password).
    if (phoneDigits.length < 10 || phoneDigits.length > 15) {
        alert("Please enter a valid phone number (10 to 15 digits).");
        return false;
    }

    if (password.length < 8) {
        alert("Password must be at least 8 characters long.");
        return false;
    }

    return true;
}
