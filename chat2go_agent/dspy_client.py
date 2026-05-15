"""
Lightweight client for the DSPy Memory Service (http://localhost:7788).
Usage in bridge.py:

    from chat2go_agent.dspy_client import dspy_ask, dspy_remember, dspy_extract

Call dspy_extract() after each assistant reply to auto-save facts.
Call dspy_ask() to enrich prompts with remembered context.
"""

import os
import logging
from typing import Optional
import requests

DSPY_BASE = os.getenv("DSPY_SERVICE_URL", "http://localhost:7788")
TIMEOUT = 5  # seconds — fail fast, never block the main reply

logger = logging.getLogger(__name__)


def _post(path: str, payload: dict) -> Optional[dict]:
    try:
        r = requests.post(f"{DSPY_BASE}{path}", json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[dspy_client] {path} failed: {e}")
        return None


def dspy_ask(question: str, user_id: str = "default") -> str:
    """Query DSPy RAG. Returns answer string, or empty string on error."""
    res = _post("/ask", {"question": question, "user_id": user_id})
    return res.get("answer", "") if res else ""


def dspy_remember(text: str, user_id: str = "default", source: str = "manual") -> bool:
    """Manually store a memory fact. Returns True on success."""
    res = _post("/remember", {"text": text, "user_id": user_id, "source": source})
    return res is not None


def dspy_extract(user_message: str, assistant_reply: str, user_id: str = "default") -> str:
    """Ask DSPy to extract & auto-save a memory fact from a conversation turn.
    Returns the extracted fact (may be empty if nothing worth saving)."""
    res = _post("/extract", {
        "user_message": user_message,
        "assistant_reply": assistant_reply,
        "user_id": user_id,
    })
    return res.get("fact", "") if res else ""


def dspy_health() -> bool:
    """Quick health check."""
    try:
        r = requests.get(f"{DSPY_BASE}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False
