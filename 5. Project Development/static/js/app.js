"use strict";

/**
 * Toggle loading overlay when forms are submitted.
 */
(function registerLoadingOverlay() {
    const overlay = document.getElementById("loadingOverlay");
    if (!overlay) {
        return;
    }

    const forms = document.querySelectorAll("form[data-loading='true']");
    forms.forEach((form) => {
        form.addEventListener("submit", () => {
            overlay.classList.remove("hidden");
            overlay.setAttribute("aria-hidden", "false");
        });
    });
})();
