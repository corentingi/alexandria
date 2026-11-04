// Small progressive enhancements. Everything works without JavaScript except live search.
(function () {
  "use strict";

  // CSRF header for every htmx request (the token is in a <meta>; CSP forbids inline JS).
  document.addEventListener("htmx:configRequest", function (event) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) event.detail.headers["X-CSRF-Token"] = meta.content;
  });

  // htmx does not swap error responses, so a failed Keep or Archive did nothing visible.
  // Say what happened instead: the server's own words when it sends plain text.
  function toast(message) {
    var box = document.createElement("div");
    box.className = "notice bad toast";
    box.setAttribute("role", "alert");
    box.textContent = message;
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 7000);
  }
  var STATUS_TEXT = {
    401: "Your session has ended: sign in again.",
    403: "Not allowed. Your session may have expired: reload the page.",
    404: "Not found: it may have been deleted. Reload the page.",
    413: "That is too large."
  };
  document.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail.xhr;
    var plain = (xhr.getResponseHeader("content-type") || "").indexOf("text/plain") === 0;
    toast(plain && xhr.responseText ? xhr.responseText.slice(0, 300)
      : STATUS_TEXT[xhr.status] || "Something went wrong (HTTP " + xhr.status + "). Try again.");
  });
  document.addEventListener("htmx:sendError", function () {
    toast("Cannot reach the server. Check the connection and try again.");
  });

  // A polled region is not refreshed while focus is inside it: the swap would take the
  // focus away (a keyboard user on a Retry button would lose their place).
  document.addEventListener("htmx:beforeRequest", function (event) {
    var region = event.detail.elt;
    var trigger = region.getAttribute && region.getAttribute("hx-trigger") || "";
    if (trigger.indexOf("every ") === 0 && region.contains(document.activeElement)
        && document.activeElement !== document.body) event.preventDefault();
  });

  // Inbox triage from the keyboard: j/k move between cards, w writes a reason (Enter in
  // it keeps), a archives, o opens, Escape returns from the field to the card. After a
  // card is kept or archived, focus moves on to the next one still waiting.
  function inboxCards() {
    return Array.prototype.filter.call(
      document.querySelectorAll(".inbox-item"), function (c) { return !c.classList.contains("done"); });
  }
  function focusCard(card, field) {
    if (!card) return;
    var input = field && card.querySelector("input[name=why]");
    (input || card).focus();
    card.scrollIntoView({ block: "nearest" });
  }
  document.addEventListener("keydown", function (event) {
    if (!document.querySelector(".inbox-item") || event.ctrlKey || event.metaKey || event.altKey) return;
    var target = event.target;
    var card = target.closest && target.closest(".inbox-item");
    if (event.key === "Escape" && card && target.tagName === "INPUT") { card.focus(); return; }
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable) return;
    var cards = inboxCards();
    var at = cards.indexOf(card);
    if (event.key === "j" || event.key === "ArrowDown") { focusCard(cards[Math.min(at + 1, cards.length - 1)]); }
    else if (event.key === "k" || event.key === "ArrowUp") { focusCard(cards[Math.max(at - 1, 0)]); }
    else if (event.key === "w" && card) { focusCard(card, true); }
    else if (event.key === "a" && card) {
      var archive = card.querySelector("form[action$='/status/archived'] button");
      if (archive) archive.click();
    }
    else if (event.key === "o" && card) {
      var link = card.querySelector("a.title");
      if (link) window.location.href = link.href;
    }
    else return;
    event.preventDefault();
  });
  var acted = null;
  document.addEventListener("htmx:beforeRequest", function (event) {
    var card = event.detail.elt.closest && event.detail.elt.closest(".inbox-item");
    acted = card ? card.id : null;
  });
  document.addEventListener("htmx:afterSettle", function () {
    if (!acted) return;
    var done = document.getElementById(acted);
    acted = null;
    if (!done || !done.classList.contains("done")) return;  // refused: the card stays
    var count = document.querySelector('.nav a[href="/inbox"] .count');
    if (count) {
      var left = parseInt(count.textContent, 10) - 1;
      if (left > 0) count.textContent = String(left); else count.remove();
    }
    var next = done.nextElementSibling;
    while (next && (!next.classList.contains("inbox-item") || next.classList.contains("done"))) {
      next = next.nextElementSibling;
    }
    focusCard(next || inboxCards()[0], true);
  });

  // Tag fields complete the tag being typed from the tags in use (so agents and people
  // reuse "mcp" instead of adding "MCP"). The suggestions are the whole field's value
  // with the last tag completed, so picking one keeps the tags before it.
  document.addEventListener("input", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("input[data-tag-input]")) return;
    var list = document.getElementById("tag-options");
    if (!list) return;
    var all = (list.getAttribute("data-all") || "").split(" ").filter(Boolean);
    var value = input.value;
    var cut = Math.max(value.lastIndexOf(" "), value.lastIndexOf(","));
    var before = value.slice(0, cut + 1);
    var typed = value.slice(cut + 1).toLowerCase();
    var used = before.toLowerCase().split(/[\s,]+/);
    var matches = all.filter(function (t) {
      return used.indexOf(t) < 0 && (!typed || (t.indexOf(typed) === 0 && t !== typed));
    }).slice(0, 12);
    list.textContent = "";
    matches.forEach(function (t) {
      var option = document.createElement("option");
      option.value = before + t;
      list.appendChild(option);
    });
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
