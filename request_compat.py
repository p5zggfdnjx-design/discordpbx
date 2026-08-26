"""Small aiohttp compatibility hooks for the operator API."""

from __future__ import annotations

from aiohttp.web_request import BaseRequest


_INSTALLED = False


def install_cached_request_body_compat() -> None:
    """Keep cached request bodies logically readable after audit middleware reads them.

    ``BaseRequest.read()`` caches bytes in ``_read_bytes``.  Aiohttp's stock
    ``can_read_body`` only checks whether the underlying payload stream is at EOF,
    so after audit middleware reads a JSON body it becomes False even though
    ``request.json()`` can still parse the cached bytes.  Several API handlers use
    ``can_read_body`` as a guard.  Make that guard reflect the cached-body behavior.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    original = BaseRequest.can_read_body.fget
    if original is None:
        return

    def can_read_body_or_cache(request: BaseRequest) -> bool:
        return getattr(request, "_read_bytes", None) is not None or bool(original(request))

    BaseRequest.can_read_body = property(
        can_read_body_or_cache,
        doc=BaseRequest.can_read_body.__doc__,
    )
    _INSTALLED = True
