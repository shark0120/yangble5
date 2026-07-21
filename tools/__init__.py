"""yangble5 measurement and compatibility tools.

Every module in this package is standard-library only and runnable as a plain
script (``python tools/cache_bench.py``) so it can be copied into an operator's
machine on its own. The package exists mainly so the pure, side-effect-free
helpers can be imported and unit-tested without a running proxy.
"""

__all__ = ["cache_bench", "cache_stats_sidecar", "claude_shim", "sitecheck"]
