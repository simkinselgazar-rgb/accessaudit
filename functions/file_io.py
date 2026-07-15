"""Shared file I/O helpers — primarily JSON loading and writing.

Built to enforce the CLAUDE.md rule: malformed JSON files MUST log
the path so the operator can find and fix them. The naive pattern
`json.loads(open(p).read())` wrapped in `except Exception: pass` hides
exactly the kind of corruption auditors need to know about.

Use `load_json` when a missing or malformed file should be a hard
failure (config files, expected pipeline artifacts). Use
`load_json_or` when a missing file is a normal "feature not run"
signal but a malformed file is still a bug worth logging.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PathLike = str | os.PathLike[str]


def load_json(path: PathLike) -> Any:
    """Load JSON from `path`. Raises `FileNotFoundError` if absent and
    `json.JSONDecodeError` if malformed. The path is logged on
    decode-error so the operator can find the corrupt file.
    """
    p = Path(path)
    try:
        with p.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        logger.exception("JSON decode failed for %s", p)
        raise


def load_json_or(path: PathLike, default: Any = None) -> Any:
    """Load JSON from `path` if it exists and is valid, else `default`.

    Missing files return `default` silently — they're a normal "feature
    not run" signal. Malformed files return `default` but ALWAYS log
    the path at WARNING with traceback, per CLAUDE.md.
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "JSON load failed for %s -- returning default; investigate the file",
            p,
            exc_info=True,
        )
        return default


def dump_json(
    path: PathLike,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    default: Any = None,
) -> None:
    """Write `data` as JSON to `path`. Creates parent directory if missing.

    `default` is the json.dump fallback for non-serializable values (e.g.
    `lambda o: getattr(o, "value", str(o))` for enum members).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=ensure_ascii, default=default)


def read_text_or(path: PathLike, default: str = "") -> str:
    """Read a UTF-8 text file if it exists, else `default`. Logs malformed
    reads (rare for plain text, but covers permission / encoding issues).
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Text read failed for %s", p, exc_info=True)
        return default
