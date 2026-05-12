from __future__ import annotations

import secrets
import threading
from typing import Any

from app.models import ChatMessage, MasterJSON, default_master_for_empty, master_for_defi_seed


class SessionState:
    def __init__(self, master: MasterJSON | None = None) -> None:
        self.messages: list[ChatMessage] = []
        self.master: MasterJSON = master or default_master_for_empty()


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: str) -> SessionState:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionState()
            return self._sessions[session_id]

    def create_with_id(self, session_id: str, state: SessionState) -> None:
        with self._lock:
            self._sessions[session_id] = state

    def new_id(self) -> str:
        return secrets.token_urlsafe(16)


store = SessionStore()


def seed_defi_session() -> tuple[str, SessionState]:
    """Create seeded multi-turn context and Master JSON (DeFi demo)."""
    sid = store.new_id()
    state = SessionState(master=master_for_defi_seed())
    state.messages = [
        ChatMessage(
            role="user",
            content="I'm collecting suspicious DeFi pool addresses. Each contributor submits one pool with chain, address, and why they think it's a scam.",
        ),
        ChatMessage(
            role="assistant",
            content="Created 3 fields: pool_address (EVM address, required), chain (select, required), evidence_url (url). Want a free-text reason field too?",
        ),
        ChatMessage(
            role="user",
            content="Yes, add a notes field. Also chain should be limited to Ethereum, Base, and Arbitrum.",
        ),
        ChatMessage(
            role="assistant",
            content="Added notes (long text). Restricted chain to those 3 options.",
        ),
    ]
    store.create_with_id(sid, state)
    return sid, state
