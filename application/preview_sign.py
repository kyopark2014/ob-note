"""Short-lived signed URLs for MCP App note previews (no public share token).

The MCP App iframe cannot send the user's session cookie, so images/videos of a note
being previewed are served from ``/s/_preview/raw`` with an HMAC signature that binds
(user, note, asset, expiry). Nothing is persisted; links die after ``TTL_SECONDS``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

from application import session_cookie

TTL_SECONDS = 3600
_DOMAIN = b"ob-note-mcp-preview-v1\n"
# Only media may be served through preview links (never notes or arbitrary files).
MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".avif",
    ".mp4", ".webm", ".m4v", ".mov",
}


def _mac(user: str, doc: str, path: str, exp: int) -> str:
    msg = _DOMAIN + "\n".join((user, doc, path, str(exp))).encode("utf-8")
    return hmac.new(session_cookie.get_signing_key(), msg, hashlib.sha256).hexdigest()


def build_url(base_url: str, user: str, doc: str, path: str, *, ttl: int = TTL_SECONDS) -> str:
    exp = int(time.time()) + ttl
    sig = _mac(user, doc, path, exp)
    return (
        f"{base_url.rstrip('/')}/s/_preview/raw"
        f"?u={quote(user, safe='')}&doc={quote(doc, safe='')}&path={quote(path, safe='')}"
        f"&exp={exp}&sig={sig}"
    )


def verify(user: str, doc: str, path: str, exp: int, sig: str) -> bool:
    if not user or not doc or not path or exp < int(time.time()):
        return False
    return hmac.compare_digest(_mac(user, doc, path, exp), sig or "")
