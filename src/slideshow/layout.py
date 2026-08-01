"""Pure timeline layout: where every clip lands and how long overlaps are.

This module answers "what goes where" without touching Resolve, so the
arithmetic that makes transitions possible is fully unit-testable.

Layout model
------------

Every slide is split into up to three timeline clips, cut at the frames where
its transitions start and end::

            slide 0        slide 1        slide 2        slide 3
    V2                     ┌────┐         ┌────┐         ┌────┐
    V1  ┌──────────────────┼────┴─────────┼────┴─────────┼────┴───────┐
        └──────────────────┴──────────────┴──────────────┴────────────┘
                           ^^^^^^         ^^^^^^
                           overlap = transition duration

* The **head** sits on V2 and covers the incoming overlap — the window in
  which the transition animates.
* The **body** sits on V1 and covers the rest of the slide.
* The **tail** sits on V2 and covers the outgoing overlap. See below.
* A slide with neither is a single ``WHOLE`` clip on V1.

Two properties fall out of cutting at the overlap boundaries, and both
matter:

1. **V1 is contiguous** — bodies butt up end-to-end with no gaps and no
   collisions, because a slide's body starts exactly where its predecessor
   finishes.
2. **Whichever slide the transition animates is the one on top.**
   Alternating V1/V2 could not manage that — the incoming slide landed
   underneath for every other transition, where animating it is invisible
   and renders as a hard cut.

Which side goes on top
----------------------

Most transitions animate the *incoming* slide over the settled one below, so
the default is a head on V2. But some only work the other way round: a page
peeling away to reveal the next photo underneath has to be the *outgoing*
slide that moves, and it has to be on top to be seen at all.

So each boundary puts exactly one of the two slides on V2:

* **incoming on top** (default) — the next slide gets a HEAD.
* **outgoing on top** — the previous slide gets a TAIL, and the next slide
  starts on V1 at the overlap, unsplit at that end.

``plan_layout``'s ``prefers_outgoing_on_top`` predicate decides per boundary;
:func:`slideshow.transitions.wants_outgoing_on_top` is the implementation the
builder passes in. Layout stays ignorant of what any given transition does —
it just needs to know which side moves.

Either way the arithmetic holds: exactly ``overlap`` frames of one slide sit
on V2 above the other, so V1 stays contiguous and V2 never collides with
itself. A slide can be split at both ends (TAIL from the boundary before it
puts *it* on V1 through the overlap, HEAD from the boundary after it), which
is why a body can carry both a lead-in and a lead-out.

The split frames are ones where the slide is static and fully opaque (its
incoming animation has just finished, or its outgoing one has yet to start),
so the cuts are invisible.

A consequence of the split is that ``TimelineLayout.clips`` holds
*segments*, not slides: up to three entries can share the same ``index``. Use
:meth:`TimelineLayout.segments_for_index` to get them all.

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
from typing import Callable, List, Optional, Sequence

from .project_model import SlideshowProject, TransitionChoice


#: Asked, per boundary, whether that transition animates the outgoing slide
#: rather than the incoming one.
OutgoingOnTopPredicate = Callable[[TransitionChoice], bool]


#: Frame rate assumed when Resolve doesn't tell us otherwise.
DEFAULT_FPS = 24.0

#: Every clip must keep at least this many frames that no transition eats
#: into, otherwise the two overlaps would meet (or cross) in the middle.
MIN_VISIBLE_FRAMES = 1

#: 1-based video track indices. Settled slides live on V1; the animating
#: head of each slide lives on V2 above it.
LOWER_TRACK = 1
UPPER_TRACK = 2

#: ``PlacedClip.segment`` values. A slide with no overlap on either side is
#: a single ``WHOLE`` clip. Otherwise it is cut into a ``BODY`` on V1 plus a
#: ``HEAD`` and/or ``TAIL`` on V2 covering the overlaps it animates through.
SEGMENT_WHOLE = "whole"
SEGMENT_HEAD = "head"
SEGMENT_BODY = "body"
SEGMENT_TAIL = "tail"


def seconds_to_frames(seconds: float, fps: float) -> int:
    """Convert a duration in seconds to a whole-frame count (round, min 1)."""
    if fps <= 0:
        raise ValueError("fps must be > 0, got {0}".format(fps))
    if seconds <= 0:
        return 1
    return max(1, int(math.floor(seconds * fps + 0.5)))


@dataclass
class PlacedClip:
    """One timeline clip: a whole slide, or one half of a split slide.

    * ``index``              — index into ``SlideshowProject.items``. All
      segments of the same slide share it.
    * ``segment``            — :data:`SEGMENT_WHOLE`, :data:`SEGMENT_HEAD`,
      :data:`SEGMENT_BODY` or :data:`SEGMENT_TAIL`.
    * ``track_index``        — 1-based video track (V1 = 1).
    * ``record_frame``       — timeline frame the clip starts on.
    * ``length_frames``      — how many frames the clip occupies.
    * ``source_start_frame`` / ``source_end_frame`` — inclusive source range
      handed to ``AppendToTimeline``.
    * ``lead_in_frames``     — frames shared with the *previous* slide; the
      transition into this clip animates over ``[0, lead_in_frames]`` in
      clip-local coordinates. Set on a HEAD, or on a BODY whose incoming
      transition put the *outgoing* slide on top instead.
    * ``lead_out_frames``    — frames shared with the *next* slide; that
      transition animates over ``[length_frames - lead_out_frames,
      length_frames]``. Set on a TAIL, or on a BODY / WHOLE whose outgoing
      transition put the *incoming* slide on top.
    """

    index: int
    track_index: int
    record_frame: int
    length_frames: int
    segment: str = SEGMENT_WHOLE
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
        """Frames during which this *segment* is the only thing on screen.

        Zero for a HEAD or a TAIL, which are entirely overlap by
        construction. For a whole-slide figure, sum the segments and use the
        slide's lead-in and lead-out.
        """
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

    ``clips`` holds timeline *segments* in placement order, so a split slide
    contributes two entries sharing one ``index``. ``transitions[i]`` is the
    (clamped) transition from slide ``i`` into slide ``i + 1``, so it always
    has ``slide_count - 1`` entries.
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
    def slide_count(self) -> int:
        """Number of source slides, as opposed to timeline segments."""
        return len(self.transitions) + 1 if self.clips else 0

    @property
    def total_frames(self) -> int:
        """Length of the finished timeline in frames."""
        if not self.clips:
            return 0
        return max(c.record_end_frame for c in self.clips)

    @property
    def total_seconds(self) -> float:
        return self.total_frames / self.fps if self.fps else 0.0

    def segments_for_index(self, index: int) -> List[PlacedClip]:
        """Every segment belonging to ``items[index]``, in placement order."""
        found = [clip for clip in self.clips if clip.index == index]
        if not found:
            raise KeyError("No placed clip for item index {0}".format(index))
        return found

    def clip_for_index(self, index: int) -> PlacedClip:
        """The first segment for ``items[index]``.

        That is the HEAD when the slide has one — the piece that carries the
        transition into it — and otherwise the BODY (or WHOLE).
        """
        return self.segments_for_index(index)[0]


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
    prefers_outgoing_on_top: Optional[OutgoingOnTopPredicate] = None,
) -> TimelineLayout:
    """Compute where every clip lands and how long each overlap really is.

    * ``fps`` — timeline frame rate; drives the seconds → frames maths.
    * ``source_frames`` — optional per-item length of the source media in
      frames (``None`` for "unbounded", which is the right answer for
      still images). A clip is never asked to play more frames than its
      source has.
    * ``prefers_outgoing_on_top`` — predicate asked, per boundary, whether
      that transition animates the *outgoing* slide rather than the incoming
      one; those boundaries get a TAIL instead of a HEAD. Defaults to "no"
      for every transition, which is the historic incoming-on-top layout.
      :func:`slideshow.transitions.wants_outgoing_on_top` is what the builder
      passes.

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
    # A hard cut must request *no* overlap regardless of the duration it
    # carries. ``TransitionChoice(kind="none", duration_frames=24)`` is easy
    # to write by accident, and honouring the 24 would carve out an overlap
    # window that nothing ever animates in — a hard cut placed 24 frames
    # early, with the slide silently losing that much screen time.
    overlaps = _clamp_overlaps(
        lengths, [0 if c.is_cut() else c.duration_frames for c in choices]
    )

    transitions = [
        replace(choice, duration_frames=d) if d != choice.duration_frames else choice
        for choice, d in zip(choices, overlaps)
    ]

    clips: List[PlacedClip] = []
    # Which side of each boundary goes on the upper track. Exactly one slide
    # per boundary is lifted onto V2, which is what keeps V1 contiguous.
    outgoing_on_top = [
        bool(d > 0 and prefers_outgoing_on_top is not None
             and prefers_outgoing_on_top(choice))
        for choice, d in zip(transitions, overlaps)
    ]

    record_frame = 0
    for i, length in enumerate(lengths):
        lead_in = overlaps[i - 1] if i > 0 else 0
        lead_out = overlaps[i] if i < len(overlaps) else 0

        # A head only exists when the boundary before this slide put the
        # incoming side on top; a tail only when the boundary after it put
        # the outgoing side on top. Whatever isn't carved off stays with the
        # body, which is why the body can end up carrying a lead-in.
        head_frames = 0 if i == 0 or outgoing_on_top[i - 1] else lead_in
        tail_frames = lead_out if (
            i < len(overlaps) and outgoing_on_top[i]
        ) else 0
        body_length = length - head_frames - tail_frames

        if head_frames > 0:
            # The overlap window, on V2, animating in over the previous
            # slide's body which is still running underneath.
            clips.append(
                PlacedClip(
                    index=i,
                    segment=SEGMENT_HEAD,
                    track_index=UPPER_TRACK,
                    record_frame=record_frame,
                    length_frames=head_frames,
                    source_start_frame=0,
                    source_end_frame=head_frames - 1,
                    lead_in_frames=head_frames,
                )
            )

        # The settled part of the slide, on V1. It starts exactly where the
        # previous slide's V1 part ends, which keeps V1 contiguous and makes
        # every cut fall on a static, opaque frame.
        clips.append(
            PlacedClip(
                index=i,
                segment=(
                    SEGMENT_WHOLE
                    if head_frames == 0 and tail_frames == 0
                    else SEGMENT_BODY
                ),
                track_index=LOWER_TRACK,
                record_frame=record_frame + head_frames,
                length_frames=body_length,
                source_start_frame=0,
                source_end_frame=body_length - 1,
                lead_in_frames=lead_in - head_frames,
                lead_out_frames=lead_out - tail_frames,
            )
        )

        if tail_frames > 0:
            # This slide is the one that animates out, so it has to be above
            # the next slide rather than under it.
            clips.append(
                PlacedClip(
                    index=i,
                    segment=SEGMENT_TAIL,
                    track_index=UPPER_TRACK,
                    record_frame=record_frame + length - tail_frames,
                    length_frames=tail_frames,
                    source_start_frame=0,
                    source_end_frame=tail_frames - 1,
                    lead_out_frames=tail_frames,
                )
            )

        record_frame += length - lead_out

    return TimelineLayout(fps=fps, clips=clips, transitions=transitions)


__all__ = [
    "DEFAULT_FPS",
    "LOWER_TRACK",
    "MIN_VISIBLE_FRAMES",
    "SEGMENT_BODY",
    "SEGMENT_HEAD",
    "SEGMENT_TAIL",
    "SEGMENT_WHOLE",
    "OutgoingOnTopPredicate",
    "PlacedClip",
    "TimelineLayout",
    "UPPER_TRACK",
    "plan_layout",
    "seconds_to_frames",
]
