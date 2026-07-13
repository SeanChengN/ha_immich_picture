"""Pure helpers for bounded Immich video cache management."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def video_files_to_evict(
    files: Iterable[Path], budget_bytes: int, protected_names: set[str]
) -> list[Path]:
    """Return least-recently-used MP4 files that exceed a cache budget."""
    candidates: list[tuple[Path, int, float]] = []
    total = 0
    for file_path in files:
        try:
            stat = file_path.stat()
        except OSError:
            continue
        total += stat.st_size
        if file_path.name not in protected_names:
            candidates.append((file_path, stat.st_size, stat.st_mtime))

    evicted: list[Path] = []
    for file_path, size, _ in sorted(candidates, key=lambda item: item[2]):
        if total <= budget_bytes:
            break
        evicted.append(file_path)
        total -= size
    return evicted
