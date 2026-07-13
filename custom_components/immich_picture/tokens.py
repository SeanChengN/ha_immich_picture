"""Pure helpers for player capability tokens."""

from __future__ import annotations

import hashlib
import hmac


def create_player_token(api_key: str, entry_id: str, salt: str = "") -> str:
    """Create a stable token that can be invalidated by changing its salt."""
    return hmac.new(
        api_key.encode(), f"{entry_id}:{salt}".encode(), hashlib.sha256
    ).hexdigest()
