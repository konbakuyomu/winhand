"""Generic engine for interactive, stateful programs and channels."""

from .manager import SessionManager
from .session import AutoReply, Session, SessionSpec
from .transports import TransportError
from .wait import wait_for

__all__ = ["AutoReply", "Session", "SessionManager", "SessionSpec", "TransportError", "wait_for"]
