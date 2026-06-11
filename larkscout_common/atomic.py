"""Atomic file writes shared across services (temp file + os.replace).

A partially written index or section file can corrupt the shared document
library; every persisted artifact goes through one of these helpers so a crash
mid-write leaves the previous file intact.
"""

import json
import os
from pathlib import Path


def _write_text(path: Path, content: str) -> None:
    """Write text atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_bytes(path: Path, content: bytes) -> None:
    """Write bytes atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)
