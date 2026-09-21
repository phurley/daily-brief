#!/usr/bin/env python3
"""URL canonicalization helpers for joining feed items to crawled pages."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

#: Markdown link; the negative lookbehind skips image links (``![alt](cdn)``).
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(\s*(https?://[^\s\)]+)\s*\)")


def _title_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unquote(text or "").lower()).strip()


def title_link(text: str | None, title: str | None, *, page_url: str | None = None) -> str | None:
    """The most specific URL for an item on a listing page.

    Listing pages link each item's own title to its detail page, while the
    extracted record often inherits the listing URL. Find the markdown link
    whose label matches the item title (exactly or as a prefix) and return it,
    skipping the page itself and image/emoji links. Returns None when there is
    no confident match, so callers keep the existing URL.
    """
    target = _title_key(title or "")
    if not text or len(target) < 6:
        return None
    page = canonical_url(page_url) if page_url else ""
    for label, url in _MD_LINK_RE.findall(text):
        # Skip image links: both plain ``![alt](cdn)`` and an image wrapped in a
        # link ``[![alt](cdn)](detail)`` show up in listing pages.
        if label.lstrip().startswith("!") or "](" in label:
            continue
        if re.search(r"\.(?:png|jpe?g|gif|webp|svg|ico|avif)(?:[?#]|$)", url, re.I):
            continue
        # Exact (normalized) match only: a prefix match could bind an item to a
        # different session of the same series.
        if _title_key(label) != target:
            continue
        if page and canonical_url(url) == page:
            continue
        return url.rstrip(".,;:)]}")
    return None

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
