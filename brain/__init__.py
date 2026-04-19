"""content-brain local persistence layer.

This package writes research reports to a personal knowledge directory
(defaults to `~/basic-memory/` to align with Basic Memory's convention).
It is intentionally decoupled from any specific indexer — the output is
plain markdown on disk, readable by Basic Memory, Obsidian, Logseq, or
plain filesystem tools.
"""

from .writer import save_research

__all__ = ["save_research"]
