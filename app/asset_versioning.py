"""Cache-busting for static assets (CSS/JS/etc).

Appends a content-hash query string to every /static/... URL rendered in
templates, e.g. /static/js/home.js?v=3f2a9c1b. The hash is derived from the
file's own bytes, so a browser that cached an old version of a file will
request a fresh copy as soon as that file changes on the server -- without
needing a full hard refresh. Files that haven't changed keep the same hash,
so normal HTTP caching still applies.

Hashes are computed once per file and cached in memory, since static files
don't change while the process is running (a new deploy/container starts a
new process, which recomputes them).
"""

import hashlib
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

_hash_cache: dict[str, str] = {}


def digest_for(path: str) -> str:
    """The content hash `asset_url` puts in a URL, for the file it names.

    Split out of asset_url because the SERVER needs to ask the same
    question from the other side: is the `v` this request carries the
    current version of this file? That is what lets /static answer
    `immutable` on the fingerprinted URL and only on that one -- see
    VersionedStaticFiles in main.py.
    """
    digest = _hash_cache.get(path)
    if digest is None:
        file_path = STATIC_DIR / path
        try:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()[:10]
        except FileNotFoundError:
            digest = "0"
        _hash_cache[path] = digest
    return digest


def asset_url(path: str) -> str:
    """Return a versioned /static/<path> URL for use in templates."""
    return f"/static/{path}?v={digest_for(path)}"


def register(templates) -> None:
    """Register asset_url() as a Jinja2 global on a Jinja2Templates instance."""
    templates.env.globals["asset_url"] = asset_url
