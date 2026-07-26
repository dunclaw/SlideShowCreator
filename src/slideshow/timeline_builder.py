"""Build a Resolve timeline from a :class:`SlideshowProject`.

Two layouts are available:

* **Overlapping** (default) — slides alternate between V1 and V2 so each
  adjacent pair overlaps by its transition duration, and every clip gets a
  Fusion comp carrying the transitions on either side of it. This is what
  makes transitions render.
* **Flat** (``overlap=False``) — the original MVP: one track, hard cuts,
  built with a single ``CreateTimelineFromClips`` call. Kept because it is
  a useful A/B baseline when a build misbehaves in Resolve.

The arithmetic lives in :mod:`slideshow.layout`, the transition planning in
:mod:`slideshow.transitions`, and the Fusion graph construction in
:mod:`slideshow.transitions.applier` — this module is only the Resolve
plumbing that joins them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from .layout import TimelineLayout, plan_layout, seconds_to_frames
from .project_model import MediaItem, SlideshowProject, TransitionChoice
from .resolve_bridge import ResolveContext
from .transitions import (
    TransitionPlan,
    apply_comp_spec,
    comp_spec_for_clip,
    plan_transition,
)


DEFAULT_TIMELINE_FPS = 24.0

#: ``kind="auto"`` needs the auto-mix planner, which doesn't exist yet. Until
#: it does, an auto transition builds as this kind rather than failing the
#: whole build.
AUTO_FALLBACK_KIND = "dissolve"


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


#: Frames-from-seconds lives in :mod:`slideshow.layout` now; re-exported here
#: because callers (and tests) reach for it on the builder.
_seconds_to_frames = seconds_to_frames


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


def _source_frame_count(media_pool_item: Any) -> Optional[int]:
    """Frames available in the source media, or ``None`` for "unbounded".

    Stills are unbounded — they stretch to any timeline length — and Resolve
    reports them with a nominal frame count that must not be used as a
    limit. Anything we can't read confidently is treated as unbounded too:
    over-trimming a clip is a worse failure than not trimming it.
    """
    try:
        clip_type = (media_pool_item.GetClipProperty("Type") or "").lower()
    except Exception:
        clip_type = ""
    if clip_type and clip_type != "video":
        return None
    try:
        frames = int(media_pool_item.GetClipProperty("Frames"))
    except Exception:
        return None
    return frames if frames > 1 else None


def _clip_infos(
    media_items: Sequence[Any],
    project_items: Sequence[MediaItem],
    project: SlideshowProject,
    fps: float,
) -> List[dict]:
    """Build the ``clipInfo`` dicts for the flat (non-overlapping) layout."""
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


def _ensure_video_tracks(timeline: Any, count: int) -> None:
    """Add video tracks until the timeline has at least *count* of them."""
    try:
        existing = int(timeline.GetTrackCount("video"))
    except Exception:
        existing = 1
    for _ in range(max(0, count - existing)):
        timeline.AddTrack("video")


def _concrete_choice(choice: TransitionChoice) -> TransitionChoice:
    """Substitute a concrete kind for ``auto`` until the auto planner lands."""
    if choice.kind != "auto":
        return choice
    return TransitionChoice(
        kind=AUTO_FALLBACK_KIND,
        duration_frames=choice.duration_frames,
        params=dict(choice.params),
    )


@dataclass
class BuildResult:
    """What a build produced, so callers can inspect or extend it.

    ``timeline_items`` and ``media_items`` are index-aligned with
    ``project.items``; ``transition_plans[i]`` is the plan between item
    ``i`` and ``i + 1`` (``None`` for a hard cut).
    """

    timeline: Any = None
    layout: Optional[TimelineLayout] = None
    timeline_items: List[Any] = field(default_factory=list)
    media_items: List[Any] = field(default_factory=list)
    transition_plans: List[Optional[TransitionPlan]] = field(default_factory=list)
    comps_applied: int = 0

    def item_for_index(self, index: int) -> Any:
        """Return the Resolve ``TimelineItem`` for ``project.items[index]``."""
        return self.timeline_items[index]


class TimelineBuilder:
    """Builds a Resolve timeline from a :class:`SlideshowProject`."""

    def __init__(
        self,
        project: SlideshowProject,
        *,
        context: Optional[ResolveContext] = None,
        media_pool_folder: str = "SlideShowCreator",
        overlap: bool = True,
        apply_transitions: bool = True,
        respect_source_length: bool = True,
    ) -> None:
        self.project = project
        self.context = context or ResolveContext()
        self.media_pool_folder = media_pool_folder
        self.overlap = overlap
        self.apply_transitions = apply_transitions
        self.respect_source_length = respect_source_length

    # -- shared setup ------------------------------------------------------ #

    def _import(self, media_pool: Any) -> List[Any]:
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
        return media_items

    # -- flat layout (the original MVP path) ------------------------------- #

    def _build_flat(
        self, media_pool: Any, media_items: List[Any], fps: float
    ) -> BuildResult:
        clip_infos = _clip_infos(media_items, self.project.items, self.project, fps)
        timeline = media_pool.CreateTimelineFromClips(self.project.name, clip_infos)
        if timeline is None:
            raise RuntimeError(
                "MediaPool.CreateTimelineFromClips returned None for project "
                "{0!r}.".format(self.project.name)
            )
        return BuildResult(timeline=timeline, media_items=media_items)

    # -- overlapping layout ------------------------------------------------ #

    def _build_overlapping(
        self,
        media_pool: Any,
        media_items: List[Any],
        fps: float,
        resolve_project: Any,
    ) -> BuildResult:
        source_frames = None
        if self.respect_source_length:
            source_frames = [_source_frame_count(mpi) for mpi in media_items]

        layout = plan_layout(self.project, fps=fps, source_frames=source_frames)

        timeline = media_pool.CreateEmptyTimeline(self.project.name)
        if timeline is None:
            raise RuntimeError(
                "MediaPool.CreateEmptyTimeline returned None for project "
                "{0!r}.".format(self.project.name)
            )
        # AppendToTimeline targets the *current* timeline, so make ours current.
        resolve_project.SetCurrentTimeline(timeline)
        _ensure_video_tracks(timeline, layout.track_count)

        timeline_items: List[Any] = []
        for placed in layout.clips:
            info = placed.to_clip_info(media_items[placed.index])
            appended = media_pool.AppendToTimeline([info]) or []
            if not appended:
                raise RuntimeError(
                    "AppendToTimeline placed nothing for item {0} ({1!r}) at "
                    "frame {2} on V{3}.".format(
                        placed.index,
                        self.project.items[placed.index].path,
                        placed.record_frame,
                        placed.track_index,
                    )
                )
            timeline_items.append(appended[0])

        result = BuildResult(
            timeline=timeline,
            layout=layout,
            timeline_items=timeline_items,
            media_items=media_items,
        )

        if self.apply_transitions:
            self._apply_transitions(result, fps)
        return result

    def _apply_transitions(self, result: BuildResult, fps: float) -> None:
        """Plan every transition and push the merged result into each comp."""
        layout = result.layout
        plans: List[Optional[TransitionPlan]] = []
        for choice in layout.transitions:
            if choice.is_cut():
                plans.append(None)
            else:
                plans.append(plan_transition(_concrete_choice(choice), fps=fps))
        result.transition_plans = plans

        for position, placed in enumerate(layout.clips):
            lead_in_plan = plans[position - 1] if position > 0 else None
            lead_out_plan = plans[position] if position < len(plans) else None
            if lead_in_plan is None and lead_out_plan is None:
                continue
            spec = comp_spec_for_clip(
                placed, lead_in_plan=lead_in_plan, lead_out_plan=lead_out_plan
            )
            if apply_comp_spec(result.timeline_items[position], spec) is not None:
                result.comps_applied += 1

    # -- entry point ------------------------------------------------------- #

    def build(self) -> BuildResult:
        """Import the project's media and create a timeline in Resolve."""
        if not self.project.items:
            raise ValueError("SlideshowProject has no items.")

        ctx = self.context
        resolve_project = ctx.project
        media_pool = ctx.media_pool

        fps = _timeline_fps(resolve_project)
        media_items = self._import(media_pool)

        if not self.overlap:
            return self._build_flat(media_pool, media_items, fps)
        return self._build_overlapping(media_pool, media_items, fps, resolve_project)


def build_slideshow(project: SlideshowProject, **kwargs: Any) -> BuildResult:
    """Convenience helper: build the project's timeline in one call."""
    return TimelineBuilder(project, **kwargs).build()


__all__ = [
    "AUTO_FALLBACK_KIND",
    "DEFAULT_TIMELINE_FPS",
    "BuildResult",
    "TimelineBuilder",
    "build_slideshow",
]
