"""Client IP resolution and in-memory sliding-window rate limiting.

These helpers are dependency-free so they can be unit-tested without a
database or an HTTP server.
"""
import ipaddress
import threading
import time
from collections import defaultdict, deque

MAX_TRACKED_KEYS = 10_000


def normalize_ip(value):
    """Return a canonical IP string, or None when the value is not an IP."""
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return None


def resolve_client_ip(real_ip, forwarded_for, peer_address):
    """Pick the client IP without trusting client-supplied forwarding headers.

    ``edge-alb`` overwrites ``X-Real-IP`` with the TCP peer, so that header is
    authoritative. Otherwise the *last* ``X-Forwarded-For`` hop is used because
    nginx appends the address it actually saw. Anything that does not parse as
    an IP falls back to the socket peer.
    """
    candidate = normalize_ip(real_ip)
    if candidate:
        return candidate
    if forwarded_for:
        for hop in reversed(str(forwarded_for).split(",")):
            candidate = normalize_ip(hop)
            if candidate:
                return candidate
    return normalize_ip(peer_address) or "unknown"


class SlidingWindowLimiter:
    """Thread-safe attempt counter per key inside a rolling time window."""

    def __init__(self, max_keys=MAX_TRACKED_KEYS, clock=time.monotonic):
        self.max_keys = max_keys
        self._clock = clock
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key, limit, window_seconds):
        """Record an attempt and return False when the key is over the limit."""
        now = self._clock()
        with self._lock:
            self._prune(now, window_seconds)
            attempts = self._attempts[key]
            while attempts and now - attempts[0] >= window_seconds:
                attempts.popleft()
            if len(attempts) >= limit:
                return False
            attempts.append(now)
            return True

    def reset(self, key):
        with self._lock:
            self._attempts.pop(key, None)

    def _prune(self, now, window_seconds):
        if len(self._attempts) <= self.max_keys:
            return
        for key in list(self._attempts):
            attempts = self._attempts[key]
            while attempts and now - attempts[0] >= window_seconds:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(key, None)
