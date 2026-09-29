"""Simple thread-safe rate limiting for outbound download requests."""

import random
import threading
import time
from typing import Optional
from contextlib import contextmanager

THROTTLE_MARKERS = (
    "rate-limited by youtube", "http error 429", "http error 403",
    "too many requests", "reached a rate/request limit",
    "max retries reached", "sign in to confirm", "not a bot",
)

AUTH_MARKERS = ("sign in to confirm", "not a bot")

_lock = threading.Lock()
_state = {
    "rate": 0.2,             # tokens added per second (0.2 = one request per 5s)
    "capacity": 1,           # maximum burst
    "tokens": 1.0,
    "updated": time.monotonic(),
    "blocked_until": 0.0,    # cooldown deadline after a throttling response
}

def looks_throttled(text: str) -> bool:
    """True if a line of subprocess output indicates the remote side is throttling."""
    low = (text or "").lower()
    return any(m in low for m in THROTTLE_MARKERS)

def looks_like_bot_check(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in AUTH_MARKERS)

def configure(rate: float = 0.2, capacity: int = 1) -> None:
    """Set the refill rate and burst size, and reset the limiter. Call once at startup."""
    if rate <= 0:
        raise ValueError("rate must be positive")
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    with _lock:
        _state["rate"] = rate
        _state["capacity"] = capacity
    reset()

def acquire(tokens: int = 1) -> float:
    """Block until `tokens` are available. Returns how long it waited."""
    started = time.monotonic()
    with _lock:
        _state["tokens"] = min(
            _state["capacity"],
            _state["tokens"] + (started - _state["updated"]) * _state["rate"])
        _state["updated"] = started
        _state["tokens"] -= tokens
        wait = max(0.0, -_state["tokens"] / _state["rate"])
        
    deadline = started + wait
    while True:
        with _lock:
            deadline = max(deadline, _state["blocked_until"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))
    return time.monotonic() - started
        
def penalize(self, seconds: float = 60.0):
    """
    Pause all callers for 'seconds' after the remote side throttles us

    Args:
        seconds (float, optional): _description_. Defaults to 60.0.
    """
    seconds += random.uniform(0, seconds * 0.1)   # jitter so threads don't restart together
    with _lock:
        _state["blocked_until"] = max(_state["blocked_until"],
                                      time.monotonic() + seconds)
        _state["tokens"] = min(_state["tokens"], 0.0)   # drop any banked burst
        
def reset() -> None:
    with _lock:
        _state["tokens"] = float(_state["capacity"])
        _state["updated"] = time.monotonic()
        _state["blocked_until"] = 0.0

def cooldown_remaining() -> float:
    with _lock:
        return max(0.0, _state["blocked_until"] - time.monotonic())

@contextmanager
def limited():
    """`with limited():` waits for a token, then runs the block."""
    acquire()
    yield