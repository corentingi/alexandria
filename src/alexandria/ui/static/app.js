// Small progressive enhancements. Everything works without JavaScript except live search.
(function () {
  "use strict";

  // CSRF header for every htmx request (the token is in a <meta>; CSP forbids inline JS).
  document.addEventListener("htmx:configRequest", function (event) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) event.detail.headers["X-CSRF-Token"] = meta.content;
  });

  // "/" focuses the search box, Escape leaves it.
  document.addEventListener("keydown", function (event) {
    var target = event.target;
    var typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    if (event.key === "/" && !typing) {
      var box = document.getElementById("q");
      if (box) { event.preventDefault(); box.focus(); box.select(); }
      else { window.location.href = "/"; }
    }
    if (event.key === "Escape" && target && target.id === "q") target.blur();
  });

  // Copy buttons: <button data-copy="#element-id">
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    var source = document.querySelector(button.getAttribute("data-copy"));
    if (!source || !navigator.clipboard) return;
    navigator.clipboard.writeText(source.textContent.trim()).then(function () {
      var label = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = label; }, 1400);
    });
  });

  // The bookmarklet link must not navigate when clicked inside the UI.
  document.addEventListener("click", function (event) {
    if (event.target.closest(".bookmarklet")) event.preventDefault();
  });

  // Confirm buttons: <button data-confirm="Really?">
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-confirm]");
    if (button && !window.confirm(button.getAttribute("data-confirm"))) event.preventDefault();
  });
})();
