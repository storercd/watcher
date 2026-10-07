"""
Shared HTTPS helper for outbound ``urllib`` requests.

A plain ``python3`` run verifies HTTPS certs fine because the Python.org
installer wires up a trusted CA bundle (its "Install Certificates.command"
step, which installs ``certifi`` and points the stdlib ``ssl`` module at it).
A PyInstaller-frozen build has no equivalent step and no access to the
macOS Keychain's trust store either, so the embedded interpreter's default
``ssl`` context can't verify *any* real-world HTTPS cert - every such request
fails with ``CERTIFICATE_VERIFY_FAILED``, silently, since callers here treat
a failed request as "no connection" rather than an error. Building an
``SSLContext`` from ``certifi``'s bundled CA data explicitly makes
verification work the same way in the frozen app as in a dev run.
"""

from __future__ import annotations

import ssl
from typing import Optional

import certifi

_ssl_context: Optional[ssl.SSLContext] = None


def ssl_context() -> ssl.SSLContext:
    """Return a shared SSLContext that verifies against certifi's CA bundle."""
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    return _ssl_context
