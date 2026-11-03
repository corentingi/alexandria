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

  // A flash message on a page opened at a #section is moved into that section, under its
  // heading, so it is seen where the action happened (the page top is scrolled away).
  document.addEventListener("DOMContentLoaded", function () {
    var target = window.location.hash && document.getElementById(window.location.hash.slice(1));
    if (!target) return;
    var heading = target.querySelector("h2, h3");
    document.querySelectorAll(".flash").forEach(function (flash) {
      if (heading) heading.insertAdjacentElement("afterend", flash);
      else target.prepend(flash);
    });
  });

  // Drop zones: show chosen file names and highlight while dragging.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches(".dropzone input[type=file]")) return;
    var out = input.closest(".dropzone").querySelector(".files-chosen");
    if (!out) return;
    var names = Array.prototype.map.call(input.files, function (f) { return f.name; });
    out.textContent = names.length ? names.length + " file(s): " + names.join(", ") : "";
    // <form data-autosubmit>: upload as soon as files are chosen or dropped.
    var form = input.form;
    if (names.length && form && form.hasAttribute("data-autosubmit")) {
      out.textContent = "Uploading " + out.textContent + "…";
      form.classList.add("sending");
      form.submit();
    }
  });

  // Modals: <button data-dialog="#id"> opens a <dialog>; [data-dialog-close] or Escape
  // closes it. showModal() focuses the [autofocus] field, and Enter submits its form.
  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-dialog]");
    if (opener) {
      var dialog = document.querySelector(opener.getAttribute("data-dialog"));
      if (dialog && dialog.showModal) dialog.showModal();
      return;
    }
    var closer = event.target.closest("[data-dialog-close]");
    if (closer) { closer.closest("dialog").close(); return; }
    // A click on the backdrop lands on the <dialog> element itself.
    if (event.target.tagName === "DIALOG" && event.target.open) event.target.close();
  });
  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var zone = event.target.closest && event.target.closest(".dropzone");
      if (zone) zone.classList.add("over");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var zone = event.target.closest && event.target.closest(".dropzone");
      if (zone) zone.classList.remove("over");
    });
  });
})();
