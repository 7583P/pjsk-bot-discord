"""Server-enforced origin checks; signed sessions remain compatible during migration."""

import os
from urllib.parse import urlsplit
from fastapi.responses import JSONResponse


def origin_of(value):
    parts = urlsplit(value or "")
    return (
        f"{parts.scheme}://{parts.netloc}"
        if parts.scheme in ("http", "https") and parts.netloc
        else ""
    )


def check_write_origin(request, frontend_url):
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    allowed = origin_of(frontend_url)
    supplied = request.headers.get("origin")
    if supplied is not None:
        if not allowed or supplied != allowed:
            return JSONResponse({"detail": "origin-not-allowed"}, status_code=403)
    elif request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "cross-site-write-rejected"}, status_code=403)
    elif not request.headers.get("authorization", "").lower().startswith("bearer "):
        # SameSite is defense in depth, not a replacement for an Origin check.
        return JSONResponse({"detail": "origin-required"}, status_code=403)
    return None


def cookie_only():
    return os.getenv("AUTH_COOKIE_ONLY", "false").lower() == "true"
