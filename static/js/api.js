/*
 * ResQNet API client.
 *
 * Every page talks to the Django REST API through this one module. Centralising
 * it means CSRF handling, error shaping, and offline detection are written once
 * and behave identically everywhere.
 *
 * On authentication: the browser already holds a Django session cookie after
 * login, so requests authenticate automatically. The API token returned at login
 * is kept in localStorage as well, because a service worker replaying a queued
 * request in the background may not carry the session cookie.
 */
(function (global) {
  "use strict";

  const TOKEN_KEY = "resqnet.token";
  const USER_KEY = "resqnet.user";

  /* ------------------------------------------------------------ CSRF */
  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[2]) : null;
  }

  function csrfToken() {
    return getCookie("csrftoken") || "";
  }

  /* ---------------------------------------------------------- Session */
  const session = {
    get token() { return localStorage.getItem(TOKEN_KEY); },
    set token(value) {
      if (value) localStorage.setItem(TOKEN_KEY, value);
      else localStorage.removeItem(TOKEN_KEY);
    },
    get user() {
      try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
      catch (e) { return null; }
    },
    set user(value) {
      if (value) localStorage.setItem(USER_KEY, JSON.stringify(value));
      else localStorage.removeItem(USER_KEY);
    },
    clear() { this.token = null; this.user = null; },
  };

  /* ------------------------------------------------------------ Errors */
  class ApiError extends Error {
    constructor(status, payload) {
      super(ApiError.describe(status, payload));
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
      this.fields = (payload && typeof payload === "object" && !Array.isArray(payload)) ? payload : {};
    }

    /* Turn DRF's error shapes into one sentence a person can act on. */
    static describe(status, payload) {
      if (status === 429) {
        // DRF says "Request was throttled. Expected available in N seconds",
        // which reads like a fault in the site rather than a deliberate limit.
        const seconds = payload && payload.detail
          ? (String(payload.detail).match(/(\d+)\s*second/) || [])[1]
          : null;
        return "Too many attempts from this network. " +
               (seconds ? "Please wait " + seconds + " seconds and try again."
                        : "Please wait a moment and try again.");
      }
      if (!payload) return "Request failed (" + status + ").";
      if (typeof payload === "string") return payload;
      if (payload.detail) return payload.detail;
      const first = Object.keys(payload)[0];
      if (!first) return "Request failed (" + status + ").";
      const value = payload[first];
      const text = Array.isArray(value) ? value[0] : value;
      return first === "non_field_errors" ? text : first.replace(/_/g, " ") + ": " + text;
    }
  }

  /* ----------------------------------------------------------- Request */
  async function request(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers);
    const isFormData = options.body instanceof FormData;

    if (!isFormData && options.body !== undefined && typeof options.body !== "string") {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    if (["POST", "PUT", "PATCH", "DELETE"].indexOf((options.method || "GET").toUpperCase()) !== -1) {
      headers["X-CSRFToken"] = csrfToken();
    }
    if (session.token) headers["Authorization"] = "Token " + session.token;
    headers["Accept"] = "application/json";

    let response;
    try {
      response = await fetch(path, {
        method: options.method || "GET",
        headers: headers,
        body: options.body,
        credentials: "same-origin",
      });
    } catch (networkError) {
      // fetch() only rejects on a genuine network failure, which for our purposes
      // means "offline". Callers use this to divert into the local queue.
      const offline = new ApiError(0, { detail: "You appear to be offline." });
      offline.isNetworkError = true;
      throw offline;
    }

    if (response.status === 204) return null;

    let payload = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.indexOf("application/json") !== -1) {
      payload = await response.json().catch(function () { return null; });
    } else {
      payload = await response.text().catch(function () { return null; });
    }

    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        // A stale token is worse than none - it makes every later call fail.
        if (response.status === 401) session.clear();
      }
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  const api = {
    ApiError: ApiError,
    session: session,
    csrfToken: csrfToken,

    get: function (path) { return request(path, { method: "GET" }); },
    post: function (path, body) { return request(path, { method: "POST", body: body }); },
    patch: function (path, body) { return request(path, { method: "PATCH", body: body }); },
    put: function (path, body) { return request(path, { method: "PUT", body: body }); },
    del: function (path) { return request(path, { method: "DELETE" }); },
    upload: function (path, formData) { return request(path, { method: "POST", body: formData }); },

    /* ------------------------------------------------------ Endpoints */
    auth: {
      login: function (username, password) {
        return api.post("/api/auth/login/", { username: username, password: password })
          .then(function (data) {
            session.token = data.token;
            session.user = data.user;
            return data;
          });
      },
      register: function (payload) {
        return api.post("/api/auth/register/", payload).then(function (data) {
          session.token = data.token;
          session.user = data.user;
          return data;
        });
      },
      logout: function () {
        return api.post("/api/auth/logout/", {})
          .catch(function () { /* logging out locally matters more than the round trip */ })
          .then(function () { session.clear(); });
      },
      me: function () { return api.get("/api/auth/me/"); },
    },

    reports: {
      list: function (params) { return api.get("/api/reports/" + queryString(params)); },
      detail: function (id) { return api.get("/api/reports/" + id + "/"); },
      create: function (payload) { return api.post("/api/reports/", payload); },
      submit: function (id) { return api.post("/api/reports/" + id + "/submit/", {}); },
      timeline: function (id) { return api.get("/api/reports/" + id + "/timeline/"); },
      uploadPhoto: function (id, formData) { return api.upload("/api/reports/" + id + "/photos/", formData); },
      triagePreview: function (indicators) { return api.post("/api/reports/triage-preview/", indicators); },
      sync: function (reports) { return api.post("/api/reports/sync/", { reports: reports }); },
      changeStatus: function (id, target, reason) {
        return api.post("/api/reports/" + id + "/status/", { target_status: target, reason: reason });
      },
    },

    dispatch: {
      myAssignments: function () { return api.get("/api/dispatch/my-assignments/"); },
      offlinePackage: function () { return api.get("/api/dispatch/offline-package/"); },
      accept: function (id) { return api.post("/api/dispatch/assignments/" + id + "/accept/", {}); },
      candidates: function (reportId) { return api.get("/api/dispatch/candidates/" + reportId + "/"); },
      assign: function (reportId, officerId, note) {
        return api.post("/api/dispatch/assign/", { report_id: reportId, officer_id: officerId, note: note });
      },
      autoAssign: function (reportId) { return api.post("/api/dispatch/auto-assign/" + reportId + "/", {}); },
    },

    inspections: {
      start: function (assignmentId) { return api.post("/api/inspections/start/", { assignment_id: assignmentId }); },
      detail: function (id) { return api.get("/api/inspections/" + id + "/"); },
      save: function (id, payload) { return api.patch("/api/inspections/" + id + "/", payload); },
      complete: function (id) { return api.post("/api/inspections/" + id + "/complete/", {}); },
      signOff: function (id, payload) { return api.post("/api/inspections/" + id + "/sign-off/", payload); },
      uploadPhoto: function (id, formData) { return api.upload("/api/inspections/" + id + "/photos/", formData); },
      compensationPreview: function (id) { return api.get("/api/inspections/" + id + "/compensation-preview/"); },
      sync: function (inspections) { return api.post("/api/inspections/sync/", { inspections: inspections }); },
    },

    compensation: {
      claims: function (params) { return api.get("/api/compensation/claims/" + queryString(params)); },
      breakdown: function (id) { return api.get("/api/compensation/claims/" + id + "/breakdown/"); },
      adjust: function (id, amount, reason) {
        return api.post("/api/compensation/claims/" + id + "/adjust/", { amount: amount, reason: reason });
      },
      rules: function () { return api.get("/api/compensation/rules/"); },
    },

    approvals: {
      queue: function () { return api.get("/api/approvals/queue/"); },
      approve: function (reportId, reason, adjustedAmount) {
        const body = { reason: reason };
        if (adjustedAmount !== undefined && adjustedAmount !== null && adjustedAmount !== "") {
          body.adjusted_amount = adjustedAmount;
        }
        return api.post("/api/approvals/" + reportId + "/approve/", body);
      },
      reject: function (reportId, reason) { return api.post("/api/approvals/" + reportId + "/reject/", { reason: reason }); },
      review: function (reportId, reason) { return api.post("/api/approvals/" + reportId + "/review/", { reason: reason }); },
      initiatePayout: function (reportId, method) {
        return api.post("/api/approvals/" + reportId + "/payout/initiate/", { payment_method: method || "BANK_TRANSFER" });
      },
      completePayout: function (reportId) { return api.post("/api/approvals/" + reportId + "/payout/complete/", {}); },
      payouts: function (params) { return api.get("/api/approvals/payouts/" + queryString(params)); },
    },

    analytics: {
      publicStats: function () { return api.get("/api/analytics/public/"); },
      dashboard: function () { return api.get("/api/analytics/dashboard/"); },
      heatmap: function (limit) { return api.get("/api/analytics/heatmap/?limit=" + (limit || 1000)); },
      officerWorkload: function () { return api.get("/api/analytics/officer-workload/"); },
      satellite: function () { return api.get("/api/analytics/satellite/"); },
      zones: function () { return api.get("/api/analytics/zones/"); },
    },

    satellite: {
      analyses: function (params) { return api.get("/api/satellite/analyses/" + queryString(params)); },
      createAnalysis: function (formData) { return api.upload("/api/satellite/analyses/", formData); },
      runAnalysis: function (id) { return api.post("/api/satellite/analyses/" + id + "/run/", {}); },
      detections: function (params) { return api.get("/api/satellite/detections/" + queryString(params)); },
      detectionsGeoJson: function (params) { return api.get("/api/satellite/detections/geojson/" + queryString(params)); },
      assessments: function (params) { return api.get("/api/satellite/assessments/" + queryString(params)); },
      refreshAssessment: function (reportReference) {
        return api.post("/api/satellite/assessments/refresh/", { report_reference: reportReference });
      },
    },

    audit: {
      events: function (params) { return api.get("/api/audit/events/" + queryString(params)); },
      caseHistory: function (reference) { return api.get("/api/audit/case/" + encodeURIComponent(reference) + "/"); },
      actions: function () { return api.get("/api/audit/actions/"); },
    },

    officers: {
      list: function (params) { return api.get("/api/auth/officers/" + queryString(params)); },
      create: function (payload) { return api.post("/api/auth/officers/", payload); },
      availability: function (payload) { return api.patch("/api/auth/me/availability/", payload); },
    },
  };

  function queryString(params) {
    if (!params) return "";
    const pairs = Object.keys(params)
      .filter(function (key) { return params[key] !== undefined && params[key] !== null && params[key] !== ""; })
      .map(function (key) { return encodeURIComponent(key) + "=" + encodeURIComponent(params[key]); });
    return pairs.length ? "?" + pairs.join("&") : "";
  }

  global.api = api;
})(window);
