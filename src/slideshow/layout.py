"""Pure timeline layout: where every clip lands and how long overlaps are.

This module answers "what goes where" without touching Resolve, so the
arithmetic that makes transitions possible is fully unit-testable.

Layout model
------------

Slides alternate between video tracks V1 and V2::

    V2          ┌────────────┐            ┌────────────┐
    V1  ┌───────┼──┐      ┌──┼────────────┼──┐
        │ slide 0  │      │ slide 2       │  │
        └───────┼──┘      └──┼────────────┼──┘
                │ slide 1    │            │ slide 3
                └────────────┘            └────────────┘
        ^^^^^^^^^^                        ^^^^^^^^^^
        overlap = transition duration

Alternating means each adjacent pair overlaps on *different* tracks, which
is what lets a transition exist at all: during the overlap both clips are
on screen and the upper one is animated by its Fusion comp.

A consequence is that every clip is the **incoming** side of the transition
before it and the **outgoing** side of the transition after it. See
:mod:`slideshow.transitions.applier` for how the two plans get merged into
that clip's single Fusion comp.

Durations
---------

``SlideshowProject.effective_duration(item)`` is treated as the clip's full
length on the timeline, *including* the frames it shares with its
neighbours. So a run of N slides of L frames each with overlaps ``d`` is
``N*L - sum(d)`` frames long, and the fully-visible (un-overlapped) portion
of a clip is ``L - d_prev - d_next``.

Requested overlaps get clamped so every clip keeps at least
:data:`MIN_VISIBLE_FRAMES` frames to itself. Clamping is iterative because
shortening one overlap can free up budget for the next.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import List, Optional, Sequence

from .project_model import SlideshowProject, TransitionChoice


#: Frame rate assumed when Resolve doesn't tell us otherwise.
DEFAULT_FPS = 24.0

#: Every clip must keep at least this many frames that no transition eats
#: into, otherwise the two overlaps would meet (or cross) in the middle.
MIN_VISIBLE_FRAMES = 1

#: 1-based video track indices used by the alternating layout.
LOWER_TRACK = 1
UPPER_TRACK = 2


def seconds_to_frames(seconds: float, fps: float) -> int:
    """Convert a duration in seconds to a whole-frame count (round, min 1)."""
    if fps <= 0:
        raise ValueError("fps must be > 0, got {0}".format(fps))
    if seconds <= 0:
        return 1
    return max(1, int(math.floor(seconds * fps + 0.5)))


@dataclass
class PlacedClip:
    """One slide's position on the timeline.

    * ``index``              — index into ``SlideshowProject.items``.
    * ``track_index``        — 1-based video track (V1 = 1).
    * ``record_frame``       — timeline frame the clip starts on.
    * ``length_frames``      — how many frames the clip occupies.
    * ``source_start_frame`` / ``source_end_frame`` — inclusive source range
      handed to ``AppendToTimeline``.
    * ``lead_in_frames``     — frames shared with the *previous* clip; the
      transition into this clip animates over ``[0, lead_in_frames]`` in
      clip-local coordinates.
    * ``lead_out_frames``    — frames shared with the *next* clip; that
      transition animates over ``[length_frames - lead_out_frames,
      length_frames]``.
    """

    index: int
    track_index: int
    record_frame: int
    length_frames: int
    source_start_frame: int = 0
    source_end_frame: int = 0
    lead_in_frames: int = 0
    lead_out_frames: int = 0

    @property
    def record_end_frame(self) -> int:
        """Exclusive timeline frame the clip ends on."""
        return self.record_frame + self.length_frames

    @property
    def lead_out_start_frame(self) -> int:
        """Clip-local frame where the outgoing transition begins."""
        return self.length_frames - self.lead_out_frames

    @property
    def visible_frames(self) -> int:
        """Frames during which this clip is the only one on screen."""
        return self.length_frames - self.lead_in_frames - self.lead_out_frames

    def to_clip_info(self, media_pool_item: object, record_offset: int = 0) -> dict:
        """Build the ``clipInfo`` dict ``MediaPool.AppendToTimeline`` wants.

        ``record_offset`` is the timeline's own start frame. ``recordFrame`` is
        an **absolute** timeline frame, and Resolve timelines start at
        01:00:00:00 (frame 86400 at 24 fps) rather than 0, so layout frames
        must be shifted by it. Without that, clips are placed an hour before
        the start of the timeline: they exist, and the track header even counts
        them, but nothing is visible and playback does nothing.

        Deliberately omits ``startFrame``/``endFrame``. Resolve ignores both
        for stills (whose source is a single frame) and substitutes its
        "standard still duration" preference instead, so length has to be
        expressed as a mark in/out on the *MediaPoolItem* — see
        :func:`slideshow.timeline_builder.mark_source_range`.
        """
        return {
            "mediaPoolItem": media_pool_item,
            "trackIndex": self.track_index,
            "recordFrame": self.record_frame + record_offset,
            "mediaType": 1,
        }


@dataclass
class TimelineLayout:
    """The full placement plan for a project.

    ``transitions[i]`` is the (clamped) transition from ``clips[i]`` into
    ``clips[i + 1]``, so it always has ``len(clips) - 1`` entries.
    """

    fps: float = DEFAULT_FPS
    clips: List[PlacedClip] = field(default_factory=list)
    transitions: List[TransitionChoice] = field(default_factory=list)

    @property
    def track_count(self) -> int:
        """Highest video track index used (1 when nothing overlaps)."""
        if not self.clips:
            return 1
        return max(c.track_index for c in self.clips)

    @property
    def total_frames(self) -> int:
        """Length of the finished timeline in frames."""
        if not self.clips:
            return 0
        return max(c.record_end_frame for c in self.clips)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / self.fps if self.fps else 0.0

    def clip_for_index(self, index: int) -> PlacedClip:
        """Return the :class:`PlacedClip` for ``items[index]``."""
        for clip in self.clips:
            if clip.index == index:
                return clip
        raise KeyError("No placed clip for item index {0}".format(index))


def _requested_overlaps(project: SlideshowProject) -> List[TransitionChoice]:
    """Collect the transition between each adjacent pair of items."""
    return [project.transition_after(i) for i in range(len(project.items) - 1)]


def _clamp_overlaps(lengths: Sequence[int], requested: Sequence[int]) -> List[int]:
    """Shrink overlaps until every clip keeps ``MIN_VISIBLE_FRAMES`` to itself.

    Two constraints, applied until the result stops changing:

    1. An overlap can't be longer than either clip it joins (minus the
       minimum visible allowance).
    2. A clip's two overlaps together can't exceed its length (minus the
       minimum visible allowance). When they do, both shrink
       proportionally so the shape of the edit is preserved.

    Shrinking is monotonic, so the loop always terminates; the iteration
    cap is belt-and-braces.
    """
    overlaps = [max(0, int(d)) for d in requested]
    if not overlaps:
        return overlaps

    n = len(lengths)
    for i, d in enumerate(overlaps):
        cap = min(lengths[i], lengths[i + 1]) - MIN_VISIBLE_FRAMES
        overlaps[i] = max(0, min(d, cap))

    for _ in range(2 * n + 4):
        changed = False
        for i in range(n):
            prev_d = overlaps[i - 1] if i > 0 else 0
            next_d = overlaps[i] if i < len(overlaps) else 0
            budget = lengths[i] - MIN_VISIBLE_FRAMES
            total = prev_d + next_d
            if total <= budget:
                continue
            if budget <= 0:
                new_prev = new_next = 0
            else:
                scale = float(budget) / float(total)
                new_prev = int(math.floor(prev_d * scale))
                new_next = int(math.floor(next_d * scale))
                # Floor twice can leave a frame unspent; give it to the
                # longer of the two so rounding never lengthens an overlap.
                spare = budget - (new_prev + new_next)
                if spare > 0:
                    if new_prev >= new_next:
                        new_prev = min(prev_d, new_prev + spare)
                    else:
                        new_next = min(next_d, new_next + spare)
            if i > 0 and new_prev != overlaps[i - 1]:
                overlaps[i - 1] = new_prev
                changed = True
            if i < len(overlaps) and new_next != overlaps[i]:
                overlaps[i] = new_next
                changed = True
        if not changed:
            break

    return overlaps


def plan_layout(
    project: SlideshowProject,
    *,
    fps: float = DEFAULT_FPS,
    source_frames: Optional[Sequence[Optional[int]]] = None,
) -> TimelineLayout:
    """Compute where every clip lands and how long each overlap really is.

    * ``fps`` — timeline frame rate; drives the seconds → frames maths.
    * ``source_frames`` — optional per-item length of the source media in
      frames (``None`` for "unbounded", which is the right answer for
      still images). A clip is never asked to play more frames than its
      source has.

    Raises :class:`ValueError` for an empty project.
    """
    items = project.items
    if not items:
        raise ValueError("SlideshowProject has no items.")
    if source_frames is not None and len(source_frames) != len(items):
        raise ValueError(
            "source_frames has {0} entries but the project has {1} items".format(
                len(source_frames), len(items)
            )
        )

    lengths: List[int] = []
    for i, item in enumerate(items):
        length = seconds_to_frames(project.effective_duration(item), fps)
        if source_frames is not None:
            available = source_frames[i]
            if available is not None and available > 0:
                length = min(length, int(available))
        lengths.append(max(1, length))

    choices = _requested_overlaps(project)
    overlaps = _clamp_overlaps(lengths, [c.duration_frames for c in choices])

    transitions = [
        replace(choice, duration_frames=d) if d != choice.duration_frames else choice
        for choice, d in zip(choices, overlaps)
    ]

    alternating = any(d > 0 for d in overlaps)

    clips: List[PlacedClip] = []
    record_frame = 0
    for i, length in enumerate(lengths):
        lead_in = overlaps[i - 1] if i > 0 else 0
        lead_out = overlaps[i] if i < len(overlaps) else 0
        clips.append(
            PlacedClip(
                index=i,
                track_index=(
                    (UPPER_TRACK if i % 2 else LOWER_TRACK)
                    if alternating
                    else LOWER_TRACK
                ),
                record_frame=record_frame,
                length_frames=length,
                source_start_frame=0,
                source_end_frame=length - 1,
                lead_in_frames=lead_in,
                lead_out_frames=lead_out,
            )
        )
        record_frame += length - lead_out

    return TimelineLayout(fps=fps, clips=clips, transitions=transitions)


__all__ = [
    "DEFAULT_FPS",
    "LOWER_TRACK",
    "MIN_VISIBLE_FRAMES",
    "PlacedClip",
    "TimelineLayout",
    "UPPER_TRACK",
    "plan_layout",
    "seconds_to_frames",
]
