"""Lightweight project model — first slice.

Only the pieces the MVP timeline builder needs right now. The full model
(transitions, titles, audio, durations) lands in the ``project-model`` todo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class MediaItem:
    """A single piece of source media in the slideshow."""

    path: str
    duration_seconds: Optional[float] = None  # None → use slideshow default
    title_text: Optional[str] = None          # populated later by the titles feature


@dataclass
class SlideshowProject:
    """In-memory representation of a slideshow before it's pushed to Resolve."""

    name: str = "Slideshow"
    items: List[MediaItem] = field(default_factory=list)
    default_item_duration_seconds: float = 4.0
    soundtrack_path: Optional[str] = None

    @classmethod
    def from_paths(
        cls,
        paths: Sequence[str],
        *,
        name: str = "Slideshow",
        default_item_duration_seconds: float = 4.0,
    ) -> "SlideshowProject":
        return cls(
            name=name,
            items=[MediaItem(path=p) for p in paths],
            default_item_duration_seconds=default_item_duration_seconds,
        )

    def effective_duration(self, item: MediaItem) -> float:
        if item.duration_seconds is not None and item.duration_seconds > 0:
            return item.duration_seconds
        return self.default_item_duration_seconds
