#!/usr/bin/env python3
"""URL canonicalization helpers for joining feed items to crawled pages."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {
    "ref", "source", "campaign", "fbclid", "gclid", "yclid", "igshid",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "wt_mc", "oly_enc_id", "oly_anon_id",
}
_TRACKING_PREFIX = ("utm_", "mc_", "pk_", "mtm_", "hsa_", "vero_", "_ga")


def canonical_url(url: str | None) -> str:
    """Normalize a URL for equality comparison.

    Lowercases scheme/host, drops ``www.`` and default ports, strips fragments,
    removes tracking query params, and drops a trailing slash. Non-tracking
    query params are kept (sorted) because some CMSes use them for content.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    for suffix in (":80", ":443"):
        if netloc.endswith(suffix):
            netloc = netloc[: -len(suffix)]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = ""
    if parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_EXACT
            and not k.lower().startswith(_TRACKING_PREFIX)
        ]
        kept.sort()
        query = urlencode(kept)
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, query, ""))
