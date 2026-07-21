"""BYOK (bring your own key) setup for yangble5.

Not a library. ``setup.py`` in this package is an operator script that is meant
to be run directly (``python byok/setup.py``); the package marker exists so the
test suite can import its pure logic and hold the cache-preserving invariants in
place -- above all, that an alias can never be rendered into a rotating
multi-model pool.
"""
