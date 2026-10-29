"""Kuro — an agentic coding CLI over the Anthropic Messages API.

A green-on-black hacker TUI that streams model output token by token, runs
sandboxed tools behind a permission gate, and enforces an optional network
scope file for bug-bounty work.
"""

import os as _os
import ssl as _ssl

__version__ = "1.0.0"
__all__ = ["__version__"]


def _ensure_ca_bundle() -> None:
    """Give the standard library a CA bundle when the platform ships none.

    A python.org Windows install arrives without one, and urllib then fails
    every https call with CERTIFICATE_VERIFY_FAILED before a byte is sent —
    which is every web tool (``web_fetch``, ``search_web``) and every model
    catalog fetch in the harness. certifi is already in this environment
    (httpx installs it), so point ``SSL_CERT_FILE`` at its bundle. Only when
    no bundle is there to use: a cafile, a capath, or an ``SSL_CERT_FILE``
    naming a real file, always wins — so a machine verifying against a
    corporate or system store keeps doing exactly that.
    """
    paths = _ssl.get_default_verify_paths()
    cafile = _os.environ.get(paths.openssl_cafile_env) or paths.cafile
    capath = _os.environ.get(paths.openssl_capath_env) or paths.capath
    if (cafile and _os.path.exists(cafile)) or (capath and _os.path.isdir(capath)):
        return
    try:
        import certifi

        bundle = certifi.where()
    except Exception:  # noqa: BLE001 - nothing to offer; keep the platform default
        return
    if _os.path.exists(bundle):
        _os.environ["SSL_CERT_FILE"] = bundle


_ensure_ca_bundle()
