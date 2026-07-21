"""Shared security helpers.

Every place that touches user-supplied input (URLs, uploaded filenames,
review IDs used in paths) goes through these helpers so hardening lives
in one file instead of being sprinkled across routes and crawlers.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


# ─── URL / SSRF guards ──────────────────────────────────────────────────────

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when a user-supplied URL points somewhere we refuse to fetch."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for any address family/kind we refuse to reach."""
    return (
        ip.is_private           # RFC 1918, RFC 4193
        or ip.is_loopback       # 127.0.0.0/8, ::1
        or ip.is_link_local     # 169.254.0.0/16 (cloud metadata lives here)
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified    # 0.0.0.0, ::
    )


def validate_public_url(url: str, *, allow_schemes: frozenset[str] = _ALLOWED_SCHEMES) -> str:
    """Validate a URL for user-initiated crawls and fetches.

    Blocks:
    - Non-http(s) schemes (file://, gopher://, ftp://, etc.)
    - Hostnames that resolve to private, loopback, link-local, or
      reserved IP ranges (defends against SSRF to internal services and
      cloud instance metadata endpoints like 169.254.169.254).
    - Empty or malformed URLs.

    When the operator sets ``allow_private_urls`` (settings.json) or
    ``WCAG_ALLOW_PRIVATE_URLS=true``, the private/loopback IP block is
    skipped so intranet apps, staging servers, and tailnet-hosted pages
    can be audited. Scheme validation still applies. Only enable this
    on a trusted, single-operator deployment — it re-opens SSRF.

    Returns the normalized URL string. Raises UnsafeURLError on refusal.

    This is a best-effort protection. Full SSRF hardening also requires
    validating the IP at connection time (TOCTOU: DNS can change between
    validation and fetch), but getting the common cases right at request
    time still blocks direct attacks.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL is empty or not a string")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in allow_schemes:
        raise UnsafeURLError(
            f"URL scheme '{scheme or '(none)'}' is not allowed — "
            f"only {sorted(allow_schemes)} are permitted"
        )

    host = (parsed.hostname or "").strip()
    if not host:
        raise UnsafeURLError("URL has no host component")

    from config import ALLOW_PRIVATE_URLS
    if ALLOW_PRIVATE_URLS:
        return url.strip()

    # Direct IP literal
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise UnsafeURLError(f"URL host {host} is a blocked IP address")
        return url.strip()
    except ValueError:
        pass  # Not an IP literal, fall through to DNS

    # Hostname — resolve and check every answer
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Cannot resolve host '{host}': {exc}")

    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise UnsafeURLError(
                f"URL host '{host}' resolves to blocked address {addr}"
            )

    return url.strip()


# ─── Filename sanitization ──────────────────────────────────────────────────

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\- ]+")


def safe_filename(user_filename: str, *, default: str = "upload") -> str:
    """Return a safe filename derived from user input.

    Strips every path component (so "../foo" becomes "foo"), replaces
    unsafe characters, and caps total length. Always returns a
    non-empty string; falls back to ``default`` if the input sanitizes
    to nothing.

    This is for filenames the server controls (upload destination). For
    rendering untrusted text to users, use template escaping instead.
    """
    if not user_filename:
        return default

    # PurePosixPath.name discards every directory separator (unix or
    # windows style, since we normalize backslashes first) and returns
    # just the final segment — that alone defeats ../../etc/passwd.
    bare = PurePosixPath(user_filename.replace("\\", "/")).name
    if not bare or bare in (".", ".."):
        return default

    cleaned = _SAFE_FILENAME.sub("_", bare).strip("._ ")
    if not cleaned:
        return default

    # Keep a reasonable cap so downstream filesystems don't reject us.
    if len(cleaned) > 200:
        stem = PurePosixPath(cleaned).stem[:180]
        suffix = PurePosixPath(cleaned).suffix[:20]
        cleaned = f"{stem}{suffix}"

    return cleaned


# ─── Review ID validation ───────────────────────────────────────────────────

_REVIEW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")


class InvalidReviewIdError(ValueError):
    """Raised when a review_id has a disallowed shape."""


def validate_review_id(review_id: str) -> str:
    """Return the review_id unchanged if it's safe, else raise.

    Only alphanumerics, underscore, and hyphen — no path separators,
    no "..", no null bytes, no whitespace. Keeps review_id usable as a
    filesystem component without needing downstream path-traversal
    checks everywhere.
    """
    if not review_id or not isinstance(review_id, str):
        raise InvalidReviewIdError("review_id is empty or not a string")
    if not _REVIEW_ID_PATTERN.fullmatch(review_id):
        raise InvalidReviewIdError(
            f"review_id '{review_id}' contains disallowed characters"
        )
    if ".." in review_id:
        raise InvalidReviewIdError("review_id may not contain '..'")
    return review_id


# ─── Upload size gate ───────────────────────────────────────────────────────

# 100 MB is generous for a single PDF/DOCX/PPTX/XLSX plus a logo image.
# Larger docs are almost always a misupload or a zip bomb.
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class UploadTooLargeError(ValueError):
    """Raised when an uploaded file exceeds the configured maximum size."""


def validate_upload_size(content: bytes, *, max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES, label: str = "upload") -> None:
    """Reject oversized uploads before they're written to disk.

    ``content`` is the already-read bytes. Callers that stream large
    uploads should check the Content-Length header first and abort
    before buffering the whole body.
    """
    size = len(content)
    if size > max_bytes:
        raise UploadTooLargeError(
            f"{label} is {size} bytes, exceeds limit of {max_bytes} bytes"
        )


async def save_user_upload(
    upload,
    dest_dir: Path,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    label: str = "upload",
    default_name: str = "upload",
) -> Path:
    """Read a FastAPI UploadFile, validate size + filename, write to disk.

    Returns the written Path. Raises UploadTooLargeError on oversize,
    ValueError if the upload is missing content.

    Every route that accepts user-provided files should go through this
    helper so path traversal and zip-bomb protection stays uniform.
    """
    if upload is None or not getattr(upload, "filename", None):
        raise ValueError(f"{label} is missing")

    content = await upload.read()
    validate_upload_size(content, max_bytes=max_bytes, label=label)

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = safe_filename(upload.filename, default=default_name)
    dest_path = dest_dir / safe_name
    dest_path.write_bytes(content)
    return dest_path
