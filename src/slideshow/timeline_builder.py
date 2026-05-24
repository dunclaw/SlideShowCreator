"""Minimum viable timeline builder.

Given a :class:`SlideshowProject`, push its media into Resolve's Media Pool
and create a timeline where each item plays for its effective duration.

No transitions yet — that's the ``transitions-framework`` todo. This module
is the end-to-end proof that media → media pool → timeline works.
"""

from __future__ import annotations

import math
import os
from typing import Any, List, Optional, Sequence

from .project_model import MediaItem, SlideshowProject
from .resolve_bridge import ResolveContext


DEFAULT_TIMELINE_FPS = 24.0


def _timeline_fps(project: Any, fallback: float = DEFAULT_TIMELINE_FPS) -> float:
    """Look up the timeline frame rate Resolve will use for new timelines."""
    raw = project.GetSetting("timelineFrameRate")
    try:
        fps = float(raw)
        if fps > 0:
            return fps
    except (TypeError, ValueError):
        pass
    return fallback


def _seconds_to_frames(seconds: float, fps: float) -> int:
    """Convert a duration in seconds to a whole-frame count (round, min 1)."""
    if seconds <= 0:
        return 1
    return max(1, int(math.floor(seconds * fps + 0.5)))


def _media_pool_subfolder(
    media_pool: Any, name: str, *, create_if_missing: bool = True
) -> Any:
    """Find (or create) a subfolder of the media-pool root by name."""
    root = media_pool.GetRootFolder()
    for sub in root.GetSubFolderList() or []:
        if sub.GetName() == name:
            return sub
    if not create_if_missing:
        return root
    new_folder = media_pool.AddSubFolder(root, name)
    return new_folder or root


def _normalize_for_resolve(path: str) -> str:
    """Return an absolute path using forward slashes (Resolve prefers them on Windows)."""
    return os.path.abspath(path).replace("\\", "/")


def _import_media(media_pool: Any, paths: Sequence[str]) -> List[Any]:
    """Import a list of absolute file paths into the *current* media-pool folder.

    Uses ``MediaPool.ImportMedia()`` (which accepts arbitrary filesystem paths),
    NOT ``MediaStorage.AddItemListToMediaPool()`` (which only accepts paths
    under Resolve's configured Media Storage roots and pops "file does not
    exist" dialogs otherwise).

    Returns the created ``MediaPoolItem`` objects in the **same order** as
    ``paths``. We index returned items by file name to recover the order
    because Resolve's import APIs do not guarantee preservation.
    """
    if not paths:
        return []

    norm_paths = [_normalize_for_resolve(p) for p in paths]
    created = media_pool.ImportMedia(norm_paths) or []

    by_name = {}
    for item in created:
        try:
            fname = item.GetClipProperty("File Name")
        except Exception:
            fname = None
        if fname:
            by_name.setdefault(fname, []).append(item)

    ordered: List[Any] = []
    for p in norm_paths:
        bucket = by_name.get(os.path.basename(p))
        if bucket:
            ordered.append(bucket.pop(0))
        else:
            ordered.append(None)  # caller decides how to handle a missing import
    return ordered


def _clip_infos(
    media_items: Sequence[Any],
    project_items: Sequence[MediaItem],
    project: SlideshowProject,
    fps: float,
) -> List[dict]:
    """Build the list of ``clipInfo`` dicts AppendToTimeline expects."""
    infos: List[dict] = []
    for mpi, item in zip(media_items, project_items):
        if mpi is None:
            continue
        frames = _seconds_to_frames(project.effective_duration(item), fps)
        infos.append(
            {
                "mediaPoolItem": mpi,
                "startFrame": 0,
                "endFrame": frames - 1,
            }
        )
    return infos


class TimelineBuilder:
    """Builds a Resolve timeline from a :class:`SlideshowProject`."""

    def __init__(
        self,
        project: SlideshowProject,
        *,
        context: Optional[ResolveContext] = None,
        media_pool_folder: str = "SlideShowCreator",
    ) -> None:
        self.project = project
        self.context = context or ResolveContext()
        self.media_pool_folder = media_pool_folder

    def build(self) -> Any:
        """Import the project's media and create a new timeline in Resolve.

        Returns the newly-created Resolve Timeline object.
        """
        if not self.project.items:
            raise ValueError("SlideshowProject has no items.")

        ctx = self.context
        resolve_project = ctx.project
        media_pool = ctx.media_pool

        fps = _timeline_fps(resolve_project)

        folder = _media_pool_subfolder(media_pool, self.media_pool_folder)
        media_pool.SetCurrentFolder(folder)

        paths = [it.path for it in self.project.items]
        media_items = _import_media(media_pool, paths)

        missing = [
            it.path for it, mpi in zip(self.project.items, media_items) if mpi is None
        ]
        if missing:
            raise RuntimeError(
                "Failed to import {0} media file(s): {1}".format(len(missing), missing)
            )

        clip_infos = _clip_infos(media_items, self.project.items, self.project, fps)
        timeline = media_pool.CreateTimelineFromClips(self.project.name, clip_infos)
        if timeline is None:
            raise RuntimeError(
                "MediaPool.CreateTimelineFromClips returned None for project "
                "{0!r}.".format(self.project.name)
            )
        return timeline


def build_slideshow(project: SlideshowProject, **kwargs: Any) -> Any:
    """Convenience helper: build the project's timeline in one call."""
    return TimelineBuilder(project, **kwargs).build()
