"""Compatibility shim: the node interface now lives in core/interfaces.py.

Keeping the import path means every node and test that says `from nodes.base import
ClipboardNode` still works, while core/ no longer has to import the nodes package.
"""

from core.interfaces import (  # noqa: F401
    ClipboardNode, ClipCallback, DescCallback, StampCallback,
)

__all__ = ["ClipboardNode", "ClipCallback", "DescCallback", "StampCallback"]
