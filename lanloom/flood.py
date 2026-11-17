"""Flood guard for state-changing requests (a ceiling, not a brute-force counter).

The sign-in, second-factor, invitation, pairing and restore routes keep their own failure counters with growing
lock-outs (auth.LoginLimiter). This guard sits in front of every state-changing API request and only caps the
rate: per client address for the routes that work without a session, per account for everything else. A normal
person never comes close; a script that floods the app (or a stolen session that is used to hammer it) gets 429.

Memory is bounded: each key keeps at most ``limit`` time stamps, and when the table is full the keys that have
been quiet longest go first."""
import threading
import time
from collections import deque

# requests per window; generous on purpose (the web page sends a few state-changing requests per action)
PUBLIC_LIMIT = 120          # per client address and minute, routes without a session
USER_LIMIT = 600            # per account and minute, signed-in routes
WINDOW_S = 60.0


class FloodGuard:
    def __init__(self, limit: int, window: float = WINDOW_S, max_keys: int = 10000):
        self.limit = int(limit)
        self.window = float(window)
        self.max_keys = int(max_keys)
        self._lock = threading.Lock()
        self._hits = {}         # key -> deque of time stamps (oldest left)

    def hit(self, key: str, now: float = None) -> float:
        """Counts one request. Returns 0 when it may pass, else the seconds until the next one may."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    self._prune(now)
                q = self._hits[key] = deque()
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(0.001, self.window - (now - q[0]))
            q.append(now)
            return 0.0

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[k]
        if len(self._hits) >= self.max_keys:     # still full: drop the keys that were quiet longest
            order = sorted(self._hits, key=lambda k: self._hits[k][-1])
            for k in order[:max(1, len(order) // 10)]:
                del self._hits[k]

    def __len__(self) -> int:
        return len(self._hits)
