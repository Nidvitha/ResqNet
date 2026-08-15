/*
 * Small shared UI helpers.
 *
 * Deliberately not a framework. These are the handful of functions every page
 * needs: escaping user text before it reaches the DOM, formatting money and
 * dates consistently, and rendering the severity badges that carry ResQNet's
 * colour language.
 */
(function (global) {
  "use strict";

  /**
   * Escape text before inserting it into HTML.
   *
   * Report descriptions and officer remarks are written by users. Interpolating
   * them into innerHTML without escaping is stored XSS - so every dynamic string
   * in this codebase goes through here first.
   */
  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Indian-format currency, e.g. 1,35,000. */
  function money(amount) {
    const value = Number(amount || 0);
    return "₹" + value.toLocaleString("en-IN", {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
  }

  function number(value) {
    return Number(value || 0).toLocaleString("en-IN");
  }

  function date(value, withTime) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (isNaN(parsed)) return "—";
    const options = { day: "numeric", month: "short", year: "numeric" };
    if (withTime) { options.hour = "2-digit"; options.minute = "2-digit"; }
    return parsed.toLocaleDateString("en-IN", options);
  }

  function relativeTime(value) {
    if (!value) return "";
    const diff = Date.now() - new Date(value).getTime();
    const minutes = Math.round(diff / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + " min ago";
    const hours = Math.round(minutes / 60);
    if (hours < 24) return hours + " hr ago";
    const days = Math.round(hours / 24);
    return days + " day" + (days === 1 ? "" : "s") + " ago";
  }

  const SEVERITY_CLASS = {
    CRITICAL: "badge-critical",
    HIGH: "badge-high",
    MEDIUM: "badge-medium",
    LOW: "badge-low",
  };

  function severityBadge(level) {
    if (!level) return '<span class="badge badge-neutral">Not triaged</span>';
    const cls = SEVERITY_CLASS[level] || "badge-neutral";
    return '<span class="badge ' + cls + '">' + esc(level) + "</span>";
  }

  const STATUS_CLASS = {
    DRAFT: "badge-neutral",
    SUBMITTED: "badge-info",
    TRIAGED: "badge-info",
    ASSIGNED: "badge-info",
    INSPECTION_PENDING: "badge-warning",
    INSPECTED: "badge-info",
    PENDING_APPROVAL: "badge-warning",
    APPROVED: "badge-success",
    PAYOUT_PENDING: "badge-warning",
    PAYOUT_INITIATED: "badge-info",
    PAYOUT_COMPLETED: "badge-success",
    REJECTED: "badge-critical",
    NEEDS_REVIEW: "badge-warning",
    CANCELLED: "badge-neutral",
  };

  function statusBadge(status, label) {
    const cls = STATUS_CLASS[status] || "badge-neutral";
    const text = label || String(status || "").replace(/_/g, " ");
    return '<span class="badge ' + cls + '">' + esc(text) + "</span>";
  }

  /** Replace a container's contents with a loading spinner. */
  function loading(container, message) {
    const node = typeof container === "string" ? document.querySelector(container) : container;
    if (!node) return;
    node.innerHTML =
      '<div class="center" style="padding:2.5rem"><span class="spinner"></span>' +
      '<p class="muted small" style="margin-top:.75rem">' + esc(message || "Loading…") + "</p></div>";
  }

  function empty(container, title, message) {
    const node = typeof container === "string" ? document.querySelector(container) : container;
    if (!node) return;
    node.innerHTML =
      '<div class="empty"><h3>' + esc(title) + "</h3><p>" + esc(message || "") + "</p></div>";
  }

  function errorBox(container, message) {
    const node = typeof container === "string" ? document.querySelector(container) : container;
    if (!node) return;
    node.innerHTML = '<div class="alert alert-error">' + esc(message) + "</div>";
  }

  /** Show a message in a page-level alert region. */
  function flash(target, kind, message) {
    const node = typeof target === "string" ? document.querySelector(target) : target;
    if (!node) return;
    node.innerHTML = '<div class="alert alert-' + kind + '">' + esc(message) + "</div>";
    node.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function clear(target) {
    const node = typeof target === "string" ? document.querySelector(target) : target;
    if (node) node.innerHTML = "";
  }

  /** Attach field-level error messages from a DRF 400 response to a form. */
  function showFieldErrors(form, fields) {
    form.querySelectorAll(".field-error").forEach(function (node) { node.remove(); });
    form.querySelectorAll(".has-error").forEach(function (node) { node.classList.remove("has-error"); });

    Object.keys(fields || {}).forEach(function (name) {
      const input = form.querySelector('[name="' + name + '"]');
      if (!input) return;
      const wrapper = input.closest(".field") || input.parentNode;
      wrapper.classList.add("has-error");
      input.setAttribute("aria-invalid", "true");
      const message = Array.isArray(fields[name]) ? fields[name][0] : fields[name];
      const node = document.createElement("p");
      node.className = "field-error";
      node.textContent = message;
      wrapper.appendChild(node);
    });
  }

  /** Read a form into a plain object, coercing checkboxes to booleans. */
  function formData(form) {
    const data = {};
    Array.prototype.forEach.call(form.elements, function (element) {
      if (!element.name || element.disabled) return;
      if (element.type === "checkbox") data[element.name] = element.checked;
      else if (element.type === "radio") { if (element.checked) data[element.name] = element.value; }
      else if (element.type === "number") data[element.name] = element.value === "" ? 0 : Number(element.value);
      else data[element.name] = element.value;
    });
    return data;
  }

  function setBusy(button, busy, busyLabel) {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.innerHTML;
      button.innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px"></span> ' +
        esc(busyLabel || "Working…");
      button.disabled = true;
    } else {
      if (button.dataset.originalLabel) button.innerHTML = button.dataset.originalLabel;
      button.disabled = false;
    }
  }

  /* Mobile navigation toggle, present on every page. */
  document.addEventListener("DOMContentLoaded", function () {
    const toggle = document.querySelector(".nav-toggle");
    const nav = document.getElementById("site-nav");
    if (toggle && nav) {
      toggle.addEventListener("click", function () {
        const open = nav.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }
  });

  global.ui = {
    esc: esc,
    money: money,
    number: number,
    date: date,
    relativeTime: relativeTime,
    severityBadge: severityBadge,
    statusBadge: statusBadge,
    loading: loading,
    empty: empty,
    errorBox: errorBox,
    flash: flash,
    clear: clear,
    showFieldErrors: showFieldErrors,
    formData: formData,
    setBusy: setBusy,
  };
})(window);
