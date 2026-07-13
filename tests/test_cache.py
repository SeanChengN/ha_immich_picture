"""Lightweight tests that do not require a Home Assistant test environment."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "immich_picture" / "cache.py"
SPEC = importlib.util.spec_from_file_location("immich_picture_cache", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)

TOKEN_PATH = Path(__file__).parents[1] / "custom_components" / "immich_picture" / "tokens.py"
TOKEN_SPEC = importlib.util.spec_from_file_location("immich_picture_tokens", TOKEN_PATH)
assert TOKEN_SPEC is not None and TOKEN_SPEC.loader is not None
TOKENS = importlib.util.module_from_spec(TOKEN_SPEC)
TOKEN_SPEC.loader.exec_module(TOKENS)


class VideoCacheEvictionTests(unittest.TestCase):
    """Test the pure cache-budget eviction policy."""

    def test_evicts_oldest_unprotected_files_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_file = root / "old.mp4"
            new_file = root / "new.mp4"
            protected_file = root / "protected.mp4"
            for path in (old_file, new_file, protected_file):
                path.write_bytes(b"x" * 10)
            os.utime(old_file, (1, 1))
            os.utime(new_file, (2, 2))
            os.utime(protected_file, (0, 0))

            evicted = CACHE.video_files_to_evict(
                root.glob("*.mp4"), 20, {protected_file.name}
            )

            self.assertEqual(evicted, [old_file])

    def test_token_salt_rotates_the_player_capability_url(self) -> None:
        original = TOKENS.create_player_token("api-key", "entry-id")
        rotated = TOKENS.create_player_token("api-key", "entry-id", "new-salt")

        self.assertNotEqual(original, rotated)
