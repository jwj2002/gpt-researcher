"""Persist research reports to a personal knowledge base on disk."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml

DEFAULT_BRAIN_PATH = "~/basic-memory"
REINDEX_TIMEOUT_SECONDS = 60


def save_research(
    report_markdown: str,
    topic: str,
    sources: Iterable[str] = (),
    config: dict | None = None,
    *,
    reindex: bool = True,
) -> Path:
    """Save a research report to the brain as a dated, slugged markdown file.

    The file is written to `{BRAIN_PATH}/research/{YYYY-MM-DD}-{slug}.md` with
    YAML frontmatter capturing topic, date, source URLs, source count, and
    (optionally) a config snapshot. The brain path is configurable via the
    `BRAIN_PATH` environment variable and defaults to `~/basic-memory` (the
    Basic Memory convention — swap to any directory you like).

    When `reindex=True` (the default) and `basic-memory` is on PATH, the
    function triggers a reindex so the new note is queryable immediately
    from Claude Desktop via the Basic Memory MCP. Set the env var
    `BRAIN_AUTOINDEX=0` to disable reindex globally.

    Returns the absolute path of the written file.
    """
    root = _brain_root() / "research"
    root.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(topic) or "untitled"
    path = _unique_path(root, date_str, slug)

    unique_sources = list(dict.fromkeys(s for s in sources if s))
    frontmatter_fields: dict = {
        "topic": topic,
        "date": date_str,
        "source_count": len(unique_sources),
        "sources": unique_sources,
        "status": "draft_ready",
    }
    if config:
        frontmatter_fields["config"] = config

    frontmatter = "---\n" + yaml.safe_dump(
        frontmatter_fields, sort_keys=False, default_flow_style=False
    ) + "---"
    path.write_text(f"{frontmatter}\n{report_markdown}\n", encoding="utf-8")

    if reindex and os.getenv("BRAIN_AUTOINDEX", "1") != "0":
        _reindex_brain()

    return path


def _brain_root() -> Path:
    return Path(os.getenv("BRAIN_PATH", DEFAULT_BRAIN_PATH)).expanduser()


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:max_len].rstrip("-")


def _unique_path(root: Path, date_str: str, slug: str) -> Path:
    candidate = root / f"{date_str}-{slug}.md"
    counter = 2
    while candidate.exists():
        candidate = root / f"{date_str}-{slug}-{counter}.md"
        counter += 1
    return candidate


def _reindex_brain() -> None:
    """Trigger `basic-memory reindex` so new notes are searchable.

    Silent no-op if `basic-memory` isn't installed. Failures are swallowed
    intentionally — the markdown file is already on disk; a failed reindex
    just means the note won't surface in Claude Desktop until the next
    successful index.
    """
    bm = shutil.which("basic-memory")
    if not bm:
        return
    try:
        subprocess.run(
            [bm, "reindex"],
            check=False,
            capture_output=True,
            timeout=REINDEX_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
