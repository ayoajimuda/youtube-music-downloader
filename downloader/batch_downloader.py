def _backoff_seconds(self, streak: int) -> float:
    """Exponential wait after `streak` consecutive throttled links, capped."""
    return min(self.rate_limit_backoff * (2 ** (streak - 1)), self.rate_limit_max_wait)
