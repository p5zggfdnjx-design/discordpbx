"""Compatibility import for older runtime feature layers.

The v3 operator server lives in :mod:`webui`. Several long-lived hotfix/guard
modules historically imported ``webui_v3``; keep this tiny alias so those
compatibility layers resolve the canonical server without duplicating it.
"""

from webui import WebControlServer

__all__ = ["WebControlServer"]
