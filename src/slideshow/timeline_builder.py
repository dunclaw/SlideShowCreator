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
from typing import Any, List, Optional, Sequence, Tuple

from .layout import (
    LOWER_TRACK,
    SEGMENT_BODY,
    SEGMENT_HEAD,
    SEGMENT_TAIL,
    SEGMENT_WHOLE,
    TimelineLayout,
    plan_layout,
    seconds_to_frames,
)

from .project_model import (
    FramingSettings,
    MediaItem,
    SlideshowProject,
    TransitionChoice,
)
from .resolve_bridge import ResolveContext
from .transitions import (
    Framing,
    TransitionPlan,
    apply_comp_spec,
    comp_spec_for_clip,
    plan_transition,
    resolve_duration_frames,
    wants_outgoing_on_top,
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


#: Frame size assumed when Resolve will not tell us — UHD, matching the
#: project template the builder creates timelines from.
DEFAULT_FRAME_SIZE = (3840, 2160)


def _timeline_frame_size(
    project: Any, fallback: Tuple[int, int] = DEFAULT_FRAME_SIZE
) -> Tuple[int, int]:
    """Pixel size of the timeline Resolve will render.

    Needed because a clip's Fusion comp does *not* inherit it — see
    :func:`~slideshow.fusion_comps.add_canvas`.
    """
    try:
        width = int(project.GetSetting("timelineResolutionWidth"))
        height = int(project.GetSetting("timelineResolutionHeight"))
    except (TypeError, ValueError, AttributeError):
        return fallback
    if width > 0 and height > 0:
        return (width, height)
    return fallback


def _source_size(media_pool_item: Any) -> Optional[Tuple[int, int]]:
    """Native pixel size of a pool item, parsed from its ``Resolution``.

    This is deliberately read from the media pool rather than from the comp:
    ``MediaIn1.GetAttrs()["TOOLI_ImageWidth"]`` is ``None`` until the comp has
    rendered at least once, so it is useless while we are still building.
    """
    if media_pool_item is None:
        return None
    try:
        raw = media_pool_item.GetClipProperty("Resolution")
    except Exception:
        return None
    if not raw:
        return None
    try:
        width, height = (int(part) for part in str(raw).lower().split("x"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (width, height)


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


def _clip_path_key(item: Any) -> Optional[str]:
    """Normalised ``File Path`` of a ``MediaPoolItem``, for identity matching."""
    try:
        path = item.GetClipProperty("File Path")
    except Exception:
        return None
    if not path:
        return None
    return str(path).replace("\\", "/").lower()


def _index_existing_clips(media_pool: Any) -> dict:
    """Map normalised file path -> ``MediaPoolItem`` for everything already in the pool.

    ``ImportMedia`` returns an empty list for a file that is already in the
    media pool, so re-running a build over the same folder would otherwise
    fail. Walking the pool once lets us reuse those clips instead.
    """
    found = {}

    def walk(folder: Any) -> None:
        for clip in folder.GetClipList() or []:
            key = _clip_path_key(clip)
            if key and key not in found:
                found[key] = clip
        for sub in folder.GetSubFolderList() or []:
            walk(sub)

    try:
        walk(media_pool.GetRootFolder())
    except Exception:
        return {}
    return found


def _import_media(media_pool: Any, paths: Sequence[str]) -> List[Any]:
    """Import a list of absolute file paths into the *current* media-pool folder.

    Uses ``MediaPool.ImportMedia()`` (which accepts arbitrary filesystem paths),
    NOT ``MediaStorage.AddItemListToMediaPool()`` (which only accepts paths
    under Resolve's configured Media Storage roots and pops "file does not
    exist" dialogs otherwise).

    Files are imported **one call per path**, which looks wasteful but is
    load-bearing: given several consecutively numbered stills in a single
    call (``DSCF0043.JPG``, ``DSCF0044.JPG``, ``DSCF0045.JPG``), Resolve's
    auto-detection collapses them into a *single* image-sequence clip named
    ``DSCF[0043-0045].JPG``. That is catastrophic for a slideshow, where
    consecutively numbered stills are the norm rather than the exception.
    Importing one path at a time gives Resolve nothing to form a sequence
    from. See ``docs/api-notes.md``.

    Returns the ``MediaPoolItem`` objects in the **same order** as ``paths``,
    with ``None`` for any that could not be imported or found.
    """
    if not paths:
        return []

    existing = _index_existing_clips(media_pool)

    ordered: List[Any] = []
    for path in paths:
        norm = _normalize_for_resolve(path)
        key = norm.lower()

        item = existing.get(key)
        if item is None:
            created = media_pool.ImportMedia([norm]) or []
            item = created[0] if created else None
            if item is not None:
                existing[key] = item

        ordered.append(item)
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


def mark_source_range(media_pool_item: Any, frames: int) -> bool:
    """Mark ``frames`` frames of in/out on a pool item, so appends use that length.

    ``AppendToTimeline``'s ``startFrame``/``endFrame`` keys are honoured for
    video but **silently ignored for stills**: a still's source is one frame,
    so any range is out of bounds and Resolve falls back to the "standard
    still duration" user preference (120 frames at 24 fps by default). That
    fallback is what makes an overlapping layout collapse — clips end up
    longer than planned, collide, and get pushed along the track.

    ``MediaPoolItem.SetMarkInOut`` *is* honoured for stills, so it is the only
    reliable way to control slide length. Marks are set immediately before the
    append and cleared afterwards by the builder.
    """
    if frames < 1:
        return False
    try:
        return bool(media_pool_item.SetMarkInOut(0, frames - 1, "video"))
    except Exception:
        return False


def clear_source_marks(media_pool_items: Sequence[Any]) -> None:
    """Undo :func:`mark_source_range`, leaving the user's media pool as we found it."""
    for item in media_pool_items:
        if item is None:
            continue
        try:
            item.ClearMarkInOut()
        except Exception:
            pass


def _clip_infos(
    media_items: Sequence[Any],
    project_items: Sequence[MediaItem],
    project: SlideshowProject,
    fps: float,
) -> List[dict]:
    """Build the ``clipInfo`` dicts for the flat (non-overlapping) layout.

    Length comes from a mark in/out on each pool item rather than
    ``startFrame``/``endFrame``, which stills ignore — see
    :func:`mark_source_range`.
    """
    infos: List[dict] = []
    for mpi, item in zip(media_items, project_items):
        if mpi is None:
            continue
        frames = _seconds_to_frames(project.effective_duration(item), fps)
        mark_source_range(mpi, frames)
        infos.append({"mediaPoolItem": mpi, "mediaType": 1})
    return infos


def _timeline_start_frame(timeline: Any) -> int:
    """Absolute frame the timeline begins on (86400 for the usual 01:00:00:00).

    ``recordFrame`` is absolute, not relative to the timeline start, so every
    layout frame has to be shifted by this. Falls back to 0 if Resolve won't
    say — a timeline starting at 0 is the only case where that is correct, and
    it is better than refusing to build.
    """
    try:
        return int(timeline.GetStartFrame())
    except Exception:
        return 0


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


def _prefers_outgoing_on_top(choice: TransitionChoice) -> bool:
    """Layout hook: does this boundary need the outgoing slide on V2?

    Resolves ``auto`` first, so a layout decision and the plan built from it
    can't disagree about which kind is actually running.
    """
    return wants_outgoing_on_top(_concrete_choice(choice))


def _resolve_duration(choice: TransitionChoice, slide_frames: int) -> int:
    """Layout hook: how long should this boundary's overlap be?

    Resolves ``auto`` first for the same reason
    :func:`_prefers_outgoing_on_top` does — the length has to come from the
    kind that will actually run.
    """
    return resolve_duration_frames(_concrete_choice(choice), slide_frames)


def _incoming_on_top(layout: TimelineLayout, transition_index: int) -> bool:
    """Is the incoming slide on a higher track than the outgoing one?

    Derived from the real placement rather than from the transition, so
    layout and planning stay independent: whichever side ``plan_layout``
    chose to lift onto V2 is the side that gets animated, and a layout that
    stacks differently still gets correctly mirrored plans.
    """
    try:
        outgoing = layout.segments_for_index(transition_index)[-1]
        incoming = layout.segments_for_index(transition_index + 1)[0]
    except (KeyError, IndexError):
        return True
    return incoming.track_index >= outgoing.track_index


@dataclass
class BuildResult:
    """What a build produced, so callers can inspect or extend it.

    ``timeline_items`` is aligned with ``layout.clips``, which holds timeline
    *segments* — a split slide contributes two. Use :meth:`item_for_index` /
    :meth:`items_for_index` to get back to ``project.items``.
    ``media_items`` is index-aligned with ``project.items``, and
    ``transition_plans[i]`` is the plan between item ``i`` and ``i + 1``
    (``None`` for a hard cut).
    """

    timeline: Any = None
    layout: Optional[TimelineLayout] = None
    timeline_items: List[Any] = field(default_factory=list)
    media_items: List[Any] = field(default_factory=list)
    transition_plans: List[Optional[TransitionPlan]] = field(default_factory=list)
    comps_applied: int = 0

    def items_for_index(self, index: int) -> List[Any]:
        """Every Resolve ``TimelineItem`` for ``project.items[index]``."""
        if self.layout is None:
            return [self.timeline_items[index]]
        return [
            self.timeline_items[position]
            for position, placed in enumerate(self.layout.clips)
            if placed.index == index
        ]

    def item_for_index(self, index: int) -> Any:
        """The first Resolve ``TimelineItem`` for ``project.items[index]``.

        For a split slide that is the head — the piece carrying the
        transition into it.
        """
        found = self.items_for_index(index)
        if not found:
            raise KeyError("No timeline item for item index {0}".format(index))
        return found[0]


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
        clear_source_marks(media_items)
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

        layout = plan_layout(
            self.project,
            fps=fps,
            source_frames=source_frames,
            prefers_outgoing_on_top=_prefers_outgoing_on_top,
            resolve_duration=_resolve_duration,
        )

        timeline = media_pool.CreateEmptyTimeline(self.project.name)
        if timeline is None:
            raise RuntimeError(
                "MediaPool.CreateEmptyTimeline returned None for project "
                "{0!r}.".format(self.project.name)
            )
        # AppendToTimeline targets the *current* timeline, so make ours current.
        resolve_project.SetCurrentTimeline(timeline)
        _ensure_video_tracks(timeline, layout.track_count)
        origin = _timeline_start_frame(timeline)

        timeline_items: List[Any] = []
        for placed in layout.clips:
            media_item = media_items[placed.index]
            # Length must be marked on the pool item; stills ignore
            # startFrame/endFrame in clipInfo (see mark_source_range).
            mark_source_range(media_item, placed.length_frames)
            info = placed.to_clip_info(media_item, record_offset=origin)
            appended = media_pool.AppendToTimeline([info]) or []
            if not appended:
                raise RuntimeError(
                    "AppendToTimeline placed nothing for item {0} ({1!r}) at "
                    "frame {2} on V{3}.".format(
                        placed.index,
                        self.project.items[placed.index].path,
                        placed.record_frame + origin,
                        placed.track_index,
                    )
                )
            timeline_items.append(appended[0])

        clear_source_marks(media_items)

        result = BuildResult(
            timeline=timeline,
            layout=layout,
            timeline_items=timeline_items,
            media_items=media_items,
        )

        if self.apply_transitions:
            self._apply_transitions(result, fps)
        return result

    @staticmethod
    def _motion_for_segment(
        project: SlideshowProject,
        layout: TimelineLayout,
        placed: PlacedClip,
        *,
        fps: float,
    ):
        """Return the segment-local motion transform for a placed clip."""
        motion = project.motion_for(placed.index)
        if motion.is_static():
            return None
        slide_frames = seconds_to_frames(
            project.effective_duration(project.items[placed.index]),
            fps,
        )
        if slide_frames <= 0:
            return None
        full_motion = motion.plan_transform(slide_frames, fps=fps)
        if full_motion.is_empty():
            return None

        lead_in = 0
        if placed.index > 0 and placed.index - 1 < len(layout.transitions):
            lead_in = layout.transitions[placed.index - 1].duration_frames or 0
        lead_out = 0
        if placed.index < len(layout.transitions):
            lead_out = layout.transitions[placed.index].duration_frames or 0

        outgoing_on_top = [
            bool(choice.duration_frames and _prefers_outgoing_on_top(choice))
            for choice in layout.transitions
        ]
        head_frames = 0 if placed.index == 0 or outgoing_on_top[placed.index - 1] else lead_in
        tail_frames = lead_out if (
            placed.index < len(layout.transitions) and outgoing_on_top[placed.index]
        ) else 0

        if placed.segment == SEGMENT_HEAD:
            segment_start = 0
        elif placed.segment == SEGMENT_TAIL:
            segment_start = slide_frames - tail_frames
        else:
            segment_start = head_frames

        if segment_start < 0:
            segment_start = 0

        def sample(channel, frame):
            """Sample a full-slide channel at an integer frame."""
            if not channel:
                return None
            keyframes = sorted(channel, key=lambda item: int(item[0]))
            if frame <= int(keyframes[0][0]):
                return keyframes[0][1]
            if frame >= int(keyframes[-1][0]):
                return keyframes[-1][1]
            for before, after in zip(keyframes, keyframes[1:]):
                before_frame, before_value = int(before[0]), before[1]
                after_frame, after_value = int(after[0]), after[1]
                if before_frame <= frame <= after_frame:
                    fraction = (frame - before_frame) / float(after_frame - before_frame)
                    if isinstance(before_value, tuple):
                        return tuple(
                            left + (right - left) * fraction
                            for left, right in zip(before_value, after_value)
                        )
                    return before_value + (after_value - before_value) * fraction
            return keyframes[-1][1]

        def shrink(channel):
            if not channel:
                return None
            segment_end = min(
                slide_frames,
                segment_start + max(0, placed.length_frames),
            )
            local_end = max(0, segment_end - segment_start)
            result = [(0, sample(channel, segment_start))]
            result.extend(
                (int(frame) - segment_start, value)
                for frame, value in channel
                if segment_start < int(frame) < segment_end
            )
            if local_end > 0:
                result.append((local_end, sample(channel, segment_end)))
            return result

        return full_motion.__class__(
            center=shrink(full_motion.center),
            size=shrink(full_motion.size),
            angle=shrink(full_motion.angle),
            pivot=shrink(full_motion.pivot),
        )

    def _apply_transitions(self, result: BuildResult, fps: float) -> None:
        """Plan every transition and push the merged result into each comp."""
        layout = result.layout
        clips = layout.clips
        plans: List[Optional[TransitionPlan]] = []
        for index, choice in enumerate(layout.transitions):
            if choice.is_cut():
                plans.append(None)
                continue
            plans.append(
                plan_transition(
                    _concrete_choice(choice),
                    fps=fps,
                    incoming_on_top=_incoming_on_top(layout, index),
                )
            )
        result.transition_plans = plans

        framing = self.project.framing
        frame_size = _timeline_frame_size(self.context.project)

        for position, placed in enumerate(clips):
            # A head carries the transition into its slide and a tail the one
            # out of it. A body carries whichever half the boundary did not
            # lift onto V2 — which can be both, when the slide before it
            # animates out and the slide after it animates in.
            lead_in_plan = None
            if placed.lead_in_frames > 0 and placed.index > 0:
                lead_in_plan = plans[placed.index - 1]
            lead_out_plan = None
            if placed.lead_out_frames > 0 and placed.index < len(plans):
                lead_out_plan = plans[placed.index]

            motion = self._motion_for_segment(self.project, layout, placed, fps=fps)

            timeline_item = result.timeline_items[position]
            # Framing has to be applied to *every* segment of a slide, not
            # just the ones carrying a transition: a head that fills and a
            # body that fits would change size mid-slide, turning the
            # split-track seam into a visible jump.
            clip_framing = self._framing_for(
                timeline_item, framing, frame_size, placed.track_index
            )
            if (
                lead_in_plan is None
                and lead_out_plan is None
                and motion is None
                and (clip_framing is None or clip_framing.is_noop())
            ):
                continue
            spec = comp_spec_for_clip(
                placed,
                lead_in_plan=lead_in_plan,
                lead_out_plan=lead_out_plan,
                framing=clip_framing,
                motion=motion,
            )
            if apply_comp_spec(timeline_item, spec) is not None:
                result.comps_applied += 1

    @staticmethod
    def _framing_for(
        timeline_item: Any,
        settings: FramingSettings,
        frame_size: Tuple[int, int],
        track_index: int,
    ) -> Optional[Framing]:
        """Build the per-clip :class:`Framing`, or ``None`` if it can't be sized.

        Returning ``None`` when the source resolution is unreadable is
        deliberate: without it we cannot compute a fit, and guessing would
        scale the photo wrongly. Falling back to Resolve's own letterboxing
        loses the backdrop but keeps the picture correct.

        **The backdrop is painted on the lower track only.** It is opaque and
        fills the frame, so a copy of it in an upper clip's comp would hide
        the clip underneath — during a transition that blacks out the other
        half of the transition entirely. The lower track is contiguous by
        construction (``plan_layout`` guarantees it spans the whole
        timeline), so one backdrop down there is behind everything, always.
        Upper clips still get a frame-sized *transparent* canvas, because
        that is what makes their animation move in frame pixels.
        """
        try:
            media_pool_item = timeline_item.GetMediaPoolItem()
        except Exception:
            return None
        source_size = _source_size(media_pool_item)
        if source_size is None:
            return None
        on_lower_track = track_index <= LOWER_TRACK
        return Framing(
            frame_width=frame_size[0],
            frame_height=frame_size[1],
            source_width=source_size[0],
            source_height=source_size[1],
            mode=settings.mode,
            backdrop_kind=settings.backdrop if on_lower_track else "none",
            backdrop_color=settings.backdrop_color,
        )

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
    "SEGMENT_BODY",
    "SEGMENT_HEAD",
    "SEGMENT_TAIL",
    "SEGMENT_WHOLE",
    "BuildResult",
    "TimelineBuilder",
    "build_slideshow",
]
