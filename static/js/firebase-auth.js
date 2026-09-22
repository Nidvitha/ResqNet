/*
 * Firebase authentication for the browser.
 *
 * The flow, end to end:
 *
 *   1. The user signs in with Firebase (email/password, or Google).
 *   2. Firebase returns a signed ID token.
 *   3. We POST that token to /api/auth/firebase/login/.
 *   4. The server verifies it, decides the role, and issues a ResQNet session.
 *
 * Note what never crosses the wire to ResQNet: the password. Firebase handles
 * the credential; this app only ever sees a token it can verify.
 *
 * Loaded from the Firebase CDN as ES modules, so this file exposes a small
 * promise-based wrapper the ordinary scripts on the page can call.
 */
(function (global) {
  "use strict";

  var SDK = "https://www.gstatic.com/firebasejs/10.14.1/";

  var state = {
    ready: false,
    configured: false,
    error: null,
    auth: null,
    modules: null,
    // null = undetermined. Only an explicit false means Google is off.
    googleEnabled: null,
  };

  var readyPromise = null;

  /** Load the SDK and initialise, once. Resolves even when unconfigured. */
  function init() {
    if (readyPromise) return readyPromise;

    readyPromise = fetch("/api/auth/firebase/config/", { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        state.googleEnabled = payload.google_enabled;
        if (!payload.configured) {
          state.ready = true;
          state.configured = false;
          // Surface the specific missing piece rather than a generic message,
          // so the fix is obvious from the page itself.
          state.error = payload.reason || "Firebase is not configured on this server.";
          return state;
        }

        // Dynamic import of ES modules from inside a classic script.
        return Promise.all([
          import(SDK + "firebase-app.js"),
          import(SDK + "firebase-auth.js"),
        ]).then(function (mods) {
          var appModule = mods[0];
          var authModule = mods[1];

          var app = appModule.initializeApp(payload.config);
          state.auth = authModule.getAuth(app);
          state.auth.useDeviceLanguage();
          state.modules = authModule;
          state.configured = true;
          state.ready = true;
          return state;
        });
      })
      .catch(function (error) {
        state.ready = true;
        state.configured = false;
        state.error = error && error.message ? error.message : String(error);
        return state;
      });

    return readyPromise;
  }

  /** Turn Firebase's error codes into something a person can act on. */
  function describe(error) {
    var code = (error && error.code) || "";
    var map = {
      "auth/invalid-email": "That email address does not look valid.",
      "auth/user-disabled": "This account has been disabled.",
      "auth/user-not-found": "No account found with that email address.",
      "auth/wrong-password": "Incorrect email or password.",
      "auth/invalid-credential": "Incorrect email or password.",
      "auth/invalid-login-credentials": "Incorrect email or password.",
      "auth/email-already-in-use": "An account already exists with that email address.",
      "auth/weak-password": "Choose a stronger password — at least 6 characters.",
      "auth/too-many-requests": "Too many attempts. Please wait a few minutes and try again.",
      "auth/popup-closed-by-user": "The Google sign-in window was closed before finishing.",
      "auth/popup-blocked": "Your browser blocked the Google sign-in window. Allow pop-ups and retry.",
      "auth/cancelled-popup-request": "Sign-in was cancelled.",
      "auth/network-request-failed": "Could not reach Firebase. Check your connection.",
      "auth/operation-not-allowed":
        "This sign-in method is not enabled in the Firebase project. " +
        "Enable it under Authentication → Sign-in method.",
      "auth/unauthorized-domain":
        "This domain is not authorised in Firebase. Add it under " +
        "Authentication → Settings → Authorized domains.",
    };
    return map[code] || (error && error.message) || "Sign-in failed. Please try again.";
  }

  /** Hand the Firebase ID token to ResQNet and get a session back. */
  function exchange(credential) {
    return credential.user.getIdToken(/* forceRefresh */ true).then(function (idToken) {
      return global.api.post("/api/auth/firebase/login/", { id_token: idToken })
        .then(function (data) {
          global.api.session.token = data.token;
          global.api.session.user = data.user;
          return data;
        })
        .catch(function (error) {
          // ResQNet refused the identity (e.g. Google used for an officer
          // account). Sign out of Firebase too, so the browser is not left
          // holding a session the platform will not honour.
          return signOut().catch(function () {}).then(function () { throw error; });
        });
    });
  }

  function signInWithPassword(email, password) {
    return init().then(function (s) {
      if (!s.configured) throw new Error(s.error || "Firebase is not available.");
      return s.modules.signInWithEmailAndPassword(s.auth, email, password)
        .then(exchange)
        .catch(function (error) {
          if (error && error.code) throw new Error(describe(error));
          throw error;
        });
    });
  }

  function registerWithPassword(email, password, displayName) {
    return init().then(function (s) {
      if (!s.configured) throw new Error(s.error || "Firebase is not available.");
      return s.modules.createUserWithEmailAndPassword(s.auth, email, password)
        .then(function (credential) {
          if (!displayName) return credential;
          return s.modules.updateProfile(credential.user, { displayName: displayName })
            .then(function () { return credential; });
        })
        .then(exchange)
        .catch(function (error) {
          if (error && error.code) throw new Error(describe(error));
          throw error;
        });
    });
  }

  /**
   * Google sign-in.
   *
   * Offered only on the citizen tab. The restriction is not enforced here — the
   * server rejects a Google token for an officer or administrator account
   * regardless of what the interface allowed.
   */
  function signInWithGoogle() {
    return init().then(function (s) {
      if (!s.configured) throw new Error(s.error || "Firebase is not available.");
      var provider = new s.modules.GoogleAuthProvider();
      provider.setCustomParameters({ prompt: "select_account" });
      return s.modules.signInWithPopup(s.auth, provider)
        .then(exchange)
        .catch(function (error) {
          if (error && error.code) throw new Error(describe(error));
          throw error;
        });
    });
  }

  function sendPasswordReset(email) {
    return init().then(function (s) {
      if (!s.configured) throw new Error(s.error || "Firebase is not available.");
      return s.modules.sendPasswordResetEmail(s.auth, email)
        .catch(function (error) { throw new Error(describe(error)); });
    });
  }

  function signOut() {
    return init().then(function (s) {
      if (!s.configured || !s.auth) return null;
      return s.modules.signOut(s.auth);
    });
  }

  global.firebaseAuth = {
    init: init,
    isConfigured: function () { return state.configured; },
    lastError: function () { return state.error; },
    googleEnabled: function () { return state.googleEnabled; },
    signInWithPassword: signInWithPassword,
    registerWithPassword: registerWithPassword,
    signInWithGoogle: signInWithGoogle,
    sendPasswordReset: sendPasswordReset,
    signOut: signOut,
  };
})(window);
