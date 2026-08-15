/*
 * ResQNet offline layer (Section 13).
 *
 * The contract this module implements:
 *
 *   Create Report -> Internet unavailable? -> IndexedDB -> Connection returns
 *                 -> Sync Queue -> Django API
 *
 * Three pieces make that work:
 *
 *   1. `queue`  - an IndexedDB store of things waiting to be sent. Each entry is
 *      stamped with a client-generated idempotency key at the moment the user
 *      pressed Save, and that key never changes on retry. That is what lets the
 *      server recognise a replay and refuse to create a duplicate case.
 *   2. `cache`  - an IndexedDB store of data pulled down for offline reading:
 *      the officer's assignments, the citizen's own reports.
 *   3. `sync`   - drains the queue whenever the browser reports it is online.
 *
 * IndexedDB is used rather than localStorage because localStorage is limited to
 * a few megabytes, is synchronous (it blocks the UI), and stores only strings.
 */
(function (global) {
  "use strict";

  const DB_NAME = "resqnet";
  const DB_VERSION = 1;
  const STORE_QUEUE = "queue";
  const STORE_CACHE = "cache";

  let dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      if (!global.indexedDB) {
        reject(new Error("This browser does not support offline storage."));
        return;
      }
      const request = indexedDB.open(DB_NAME, DB_VERSION);

      request.onupgradeneeded = function (event) {
        const db = event.target.result;
        if (!db.objectStoreNames.contains(STORE_QUEUE)) {
          const store = db.createObjectStore(STORE_QUEUE, { keyPath: "key" });
          store.createIndex("kind", "kind", { unique: false });
          store.createIndex("createdAt", "createdAt", { unique: false });
        }
        if (!db.objectStoreNames.contains(STORE_CACHE)) {
          db.createObjectStore(STORE_CACHE, { keyPath: "name" });
        }
      };
      request.onsuccess = function (event) { resolve(event.target.result); };
      request.onerror = function () { reject(request.error); };
    });
    return dbPromise;
  }

  function tx(storeName, mode, work) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        const transaction = db.transaction(storeName, mode);
        const store = transaction.objectStore(storeName);
        let result;
        try { result = work(store); } catch (e) { reject(e); return; }
        transaction.oncomplete = function () {
          resolve(result && result.result !== undefined ? result.result : result);
        };
        transaction.onerror = function () { reject(transaction.error); };
        transaction.onabort = function () { reject(transaction.error); };
      });
    });
  }

  /* Generated once per saved item and reused on every retry - this is the whole
   * basis of duplicate protection. */
  function newIdempotencyKey() {
    if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID();
    return "k-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
  }

  const queue = {
    /** Add one item to the outbound queue. `kind` is "report" or "inspection". */
    add: function (kind, payload) {
      const entry = {
        key: payload.idempotency_key || newIdempotencyKey(),
        kind: kind,
        payload: payload,
        createdAt: new Date().toISOString(),
        attempts: 0,
        lastError: null,
      };
      entry.payload.idempotency_key = entry.key;
      return tx(STORE_QUEUE, "readwrite", function (store) { store.put(entry); })
        .then(function () { notify(); return entry; });
    },

    all: function (kind) {
      return tx(STORE_QUEUE, "readonly", function (store) { return store.getAll(); })
        .then(function (items) {
          const list = items || [];
          return kind ? list.filter(function (item) { return item.kind === kind; }) : list;
        });
    },

    count: function (kind) {
      return queue.all(kind).then(function (items) { return items.length; });
    },

    remove: function (key) {
      return tx(STORE_QUEUE, "readwrite", function (store) { store.delete(key); })
        .then(function () { notify(); });
    },

    markFailed: function (key, message) {
      return tx(STORE_QUEUE, "readwrite", function (store) {
        const request = store.get(key);
        request.onsuccess = function () {
          const entry = request.result;
          if (!entry) return;
          entry.attempts += 1;
          entry.lastError = message;
          store.put(entry);
        };
      });
    },

    clear: function () {
      return tx(STORE_QUEUE, "readwrite", function (store) { store.clear(); })
        .then(function () { notify(); });
    },
  };

  const cache = {
    put: function (name, data) {
      return tx(STORE_CACHE, "readwrite", function (store) {
        store.put({ name: name, data: data, savedAt: new Date().toISOString() });
      });
    },
    get: function (name) {
      return tx(STORE_CACHE, "readonly", function (store) { return store.get(name); })
        .then(function (row) { return row || null; });
    },
    clear: function () {
      return tx(STORE_CACHE, "readwrite", function (store) { store.clear(); });
    },
  };

  /* ------------------------------------------------------------- Sync */
  let syncing = false;

  /**
   * Send everything queued. Safe to call repeatedly - a replayed submission is
   * recognised by its idempotency key and reported as a duplicate, not stored
   * twice.
   */
  async function syncNow() {
    if (syncing || !navigator.onLine) return { synced: 0, skipped: true };
    syncing = true;
    setBanner("syncing");

    const result = { synced: 0, duplicates: 0, failed: 0 };
    try {
      const reports = await queue.all("report");
      if (reports.length) {
        const payload = reports.map(function (item) { return item.payload; });
        const response = await global.api.reports.sync(payload);
        result.synced += response.synced || 0;
        result.duplicates += response.duplicates || 0;

        const failedKeys = {};
        (response.errors || []).forEach(function (error) {
          if (error.idempotency_key) failedKeys[error.idempotency_key] = describeErrors(error.errors);
        });
        for (const item of reports) {
          if (failedKeys[item.key]) {
            result.failed += 1;
            await queue.markFailed(item.key, failedKeys[item.key]);
          } else {
            await queue.remove(item.key);
          }
        }
      }

      const inspections = await queue.all("inspection");
      if (inspections.length) {
        const payload = inspections.map(function (item) { return item.payload; });
        const response = await global.api.inspections.sync(payload);
        result.synced += response.synced || 0;
        result.duplicates += response.duplicates || 0;

        const failedKeys = {};
        (response.errors || []).forEach(function (error) {
          if (error.idempotency_key) failedKeys[error.idempotency_key] = describeErrors(error.errors);
        });
        for (const item of inspections) {
          if (failedKeys[item.key]) {
            result.failed += 1;
            await queue.markFailed(item.key, failedKeys[item.key]);
          } else {
            await queue.remove(item.key);
          }
        }
      }
    } catch (error) {
      // Still offline, or the server is unreachable. Everything stays queued.
      result.failed = -1;
    } finally {
      syncing = false;
      notify();
      updateBanner();
    }
    return result;
  }

  function describeErrors(errors) {
    if (!errors) return "Unknown error";
    if (typeof errors === "string") return errors;
    const first = Object.keys(errors)[0];
    if (!first) return "Unknown error";
    const value = errors[first];
    return first + ": " + (Array.isArray(value) ? value[0] : value);
  }

  /* -------------------------------------------------------- UI signals */
  const listeners = [];

  function onChange(callback) {
    listeners.push(callback);
    notify();
  }

  function notify() {
    queue.count().then(function (count) {
      listeners.forEach(function (callback) {
        try { callback({ pending: count, online: navigator.onLine }); }
        catch (e) { /* a broken listener must not stop the others */ }
      });
      const pill = document.querySelectorAll("[data-sync-count]");
      pill.forEach(function (node) {
        node.textContent = count;
        node.closest("[data-sync-pill]") && (node.closest("[data-sync-pill]").hidden = count === 0);
      });
    });
  }

  function setBanner(state) {
    const bar = document.getElementById("offline-bar");
    if (!bar) return;
    bar.classList.toggle("is-syncing", state === "syncing");
    if (state === "syncing") {
      bar.textContent = "Syncing saved data…";
      bar.classList.add("is-visible");
    }
  }

  function updateBanner() {
    const bar = document.getElementById("offline-bar");
    if (!bar) return;
    bar.classList.remove("is-syncing");

    queue.count().then(function (count) {
      if (!navigator.onLine) {
        bar.textContent = count
          ? "You are offline. " + count + " item(s) saved on this device and will send automatically."
          : "You are offline. You can still fill in forms — they will send when you reconnect.";
        bar.classList.add("is-visible");
      } else if (count) {
        bar.textContent = count + " item(s) waiting to sync. Tap to send now.";
        bar.classList.add("is-visible");
      } else {
        bar.classList.remove("is-visible");
      }
    });
  }

  function init() {
    updateBanner();
    global.addEventListener("online", function () { updateBanner(); syncNow(); });
    global.addEventListener("offline", updateBanner);

    const bar = document.getElementById("offline-bar");
    if (bar) {
      bar.style.cursor = "pointer";
      bar.addEventListener("click", function () { if (navigator.onLine) syncNow(); });
    }
    if (navigator.onLine) syncNow();
  }

  global.offline = {
    queue: queue,
    cache: cache,
    syncNow: syncNow,
    onChange: onChange,
    newIdempotencyKey: newIdempotencyKey,
    updateBanner: updateBanner,
    init: init,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);
