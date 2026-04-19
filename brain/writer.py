"""Persist research reports to a personal knowledge base on disk."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml

DEFAULT_BRAIN_PATH = "~/brain"


def save_research(
    report_markdown: str,
    topic: str,
    sources: Iterable[str] = (),
    config: dict | None = None,
) -> Path:
    """Save a research report to the brain as a dated, slugged markdown file.

    The file is written to `{BRAIN_PATH}/research/{YYYY-MM-DD}-{slug}.md` with
    YAML frontmatter capturing topic, date, source URLs, source count, and
    (optionally) a config snapshot. The brain path is configurable via the
    `BRAIN_PATH` environment variable and defaults to `~/brain`.

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
