"""
Authentication throttling.

Section 26 asks for rate limiting "where appropriate", and a login endpoint is
the classic place: without a limit, an attacker can try passwords as fast as the
network allows.

But the obvious implementation is wrong in a way that matters here. Counting
*every* request means a legitimate user who signs in correctly still burns
quota — and on a shared IP (an office, a relief camp behind one connection, a
developer testing locally) real users lock each other out while an attacker is
barely inconvenienced.

So `LoginRateThrottle` counts **failed attempts only**. A correct password costs
nothing. Ten wrong passwords in a minute from one address is what gets blocked,
which is the behaviour actually wanted.

Registration gets its own separate scope, so a burst of sign-ins can never
exhaust the allowance for people creating accounts, and vice versa.
"""

from rest_framework.throttling import SimpleRateThrottle


class FailureOnlyRateThrottle(SimpleRateThrottle):
    """
    Base class that checks the limit but does not consume it automatically.

    `SimpleRateThrottle.allow_request` normally records the request as soon as it
    is allowed. Here recording is deferred: the view calls `record_failure()`
    only when the attempt actually failed.
    """

    def allow_request(self, request, view):
        if self.rate is None:
            return True

        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True

        self.history = self.cache.get(self.key, [])
        self.now = self.timer()

        # Drop attempts that have aged out of the window.
        while self.history and self.history[-1] <= self.now - self.duration:
            self.history.pop()

        # Note what is missing: no throttle_success() call. Passing the check
        # costs nothing; only record_failure() consumes the allowance.
        return len(self.history) < self.num_requests

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}

    def record_failure(self, request, view) -> None:
        """Consume one attempt. Called by the view after a rejected attempt."""
        if self.rate is None:
            return
        if getattr(self, "key", None) is None:
            self.key = self.get_cache_key(request, view)
            if self.key is None:
                return
            self.history = self.cache.get(self.key, [])
            self.now = self.timer()

        self.history.insert(0, self.now)
        self.cache.set(self.key, self.history, self.duration)


class LoginRateThrottle(FailureOnlyRateThrottle):
    """Blocks password guessing without penalising anyone who signs in correctly."""

    scope = "login"


class RegistrationRateThrottle(SimpleRateThrottle):
    """
    Limits account creation per IP.

    This one counts every request, because unlike a login there is no such thing
    as a "failed" registration worth ignoring — the thing being limited is how
    many accounts one address can create, successful or not.
    """

    scope = "register"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
