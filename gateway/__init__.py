"""yangble5 public gateway.

A small, deliberately boring FastAPI service that sits between the public
internet and the *internal* yangble5 engine (CLIProxyAPI). It authenticates
yangble5-issued keys, enforces per-key quota + rate limits and a hard global
operator spend cap, then reverse-proxies the opaque request body to the engine
using a SERVER-side engine key that the caller never sees.

Nothing in this package should ever be given an upstream provider credential,
a prompt, or a completion to log.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
