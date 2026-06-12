"""Browser session object + manager (TTL/LRU eviction with cleanup callbacks).

`Session` holds a Playwright context/page plus per-session distill cache and a
concurrency lock; `SessionManager` is the TTL+maxsize store that closes the
context on eviction/expiry (fixing the resource leak a plain TTLCache had). The
process-wide `sessions` singleton is shared by all endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("larkscout_browser")

SESSION_TTL_SECONDS = 30 * 60  # 30 min idle
SESSION_MAXSIZE = 200


@dataclass
class Session:
    context: BrowserContext
    page: Page
    lang: str
    last_distill: dict[str, Any] | None = None
    action_map: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # ✅ IMPROVED: field(default_factory)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # concurrency lock
    closed: bool = False  # set when session is evicted/expired
    # WebMCP: cached tool list
    webmcp_tools: list[dict[str, Any]] | None = None
    webmcp_available: bool = False


# ============================================================
# SessionManager with expiry callbacks,
#    replaces TTLCache to fix resource leak on expired sessions
# ============================================================
class SessionManager:
    def __init__(self, ttl: int = SESSION_TTL_SECONDS, maxsize: int = SESSION_MAXSIZE):
        self._sessions: OrderedDict[str, tuple[float, Session]] = OrderedDict()
        self._ttl = ttl
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    def __len__(self):
        return len(self._sessions)

    async def put(self, sid: str, sess: Session) -> None:
        async with self._lock:
            # evict oldest
            if len(self._sessions) >= self._maxsize:
                old_sid, (_, old_sess) = self._sessions.popitem(last=False)
                logger.info("session evicted (maxsize): %s", old_sid)
                await self._close_session(old_sess)
            self._sessions[sid] = (time.time(), sess)

    async def get(self, sid: str) -> Session | None:
        async with self._lock:
            item = self._sessions.get(sid)
            if not item:
                return None
            ts, sess = item
            if time.time() - ts > self._ttl:
                del self._sessions[sid]
                logger.info("session expired on access: %s", sid)
                await self._close_session(sess)
                return None
            # refresh timestamp & move to end
            self._sessions[sid] = (time.time(), sess)
            self._sessions.move_to_end(sid)
            return sess

    async def remove(self, sid: str) -> None:
        async with self._lock:
            item = self._sessions.pop(sid, None)
            if item:
                _, sess = item
                await self._close_session(sess)

    async def cleanup(self) -> None:
        """Periodic cleanup of expired sessions."""
        async with self._lock:
            now = time.time()
            expired = [sid for sid, (ts, _) in self._sessions.items() if now - ts > self._ttl]
            for sid in expired:
                _, sess = self._sessions.pop(sid)
                logger.info("session expired (cleanup): %s", sid)
                await self._close_session(sess)

    async def close_all(self) -> None:
        async with self._lock:
            for sid, (_, sess) in self._sessions.items():
                await self._close_session(sess)
            self._sessions.clear()

    @staticmethod
    async def _close_session(sess: Session):
        sess.closed = True
        try:
            await sess.context.close()
        except Exception:
            pass


sessions = SessionManager()
