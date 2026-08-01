"""Unit tests for :mod:`slideshow.layout` — pure maths, no Resolve."""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from slideshow.layout import (
    LOWER_TRACK,
    MIN_VISIBLE_FRAMES,
    SEGMENT_BODY,
    SEGMENT_HEAD,
    SEGMENT_TAIL,
    SEGMENT_WHOLE,
    UPPER_TRACK,
    PlacedClip,
    TimelineLayout,
    plan_layout,
    seconds_to_frames,
)
from slideshow.project_model import MediaItem, SlideshowProject, TransitionChoice


def _project(count=3, *, duration=4.0, transition="dissolve", frames=24):
    return SlideshowProject(
        name="T",
        items=[MediaItem(path="s{0}.jpg".format(i)) for i in range(count)],
        default_item_duration_seconds=duration,
        default_transition=TransitionChoice(kind=transition, duration_frames=frames),
    )


def _slide_lengths(layout):
    """Total frames each *slide* occupies, summing its segments."""
    return [
        sum(c.length_frames for c in layout.segments_for_index(i))
        for i in range(layout.slide_count)
    ]


def _slide_starts(layout):
    """Timeline frame each *slide* begins on."""
    return [
        layout.segments_for_index(i)[0].record_frame
        for i in range(layout.slide_count)
    ]


def _slide_leads(layout, index):
    """``(lead_in, lead_out)`` for a slide, wherever its segments put them."""
    segments = layout.segments_for_index(index)
    return (
        max(c.lead_in_frames for c in segments),
        max(c.lead_out_frames for c in segments),
    )


# --------------------------------------------------------------------------- #
# seconds_to_frames
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "seconds, fps, expected",
    [
        (1.0, 24.0, 24),
        (0.5, 24.0, 12),
        (4.0, 30.0, 120),
        (0.0, 24.0, 1),
        (-3.0, 24.0, 1),
        (0.999999, 24.0, 24),
    ],
)
def test_seconds_to_frames(seconds, fps, expected):
    assert seconds_to_frames(seconds, fps) == expected


def test_seconds_to_frames_rejects_bad_fps():
    with pytest.raises(ValueError):
        seconds_to_frames(1.0, 0)


# --------------------------------------------------------------------------- #
# Placement basics
# --------------------------------------------------------------------------- #

def test_empty_project_raises():
    with pytest.raises(ValueError, match="no items"):
        plan_layout(SlideshowProject())


def test_single_clip_has_no_overlaps():
    layout = plan_layout(_project(1))
    assert len(layout.clips) == 1
    assert layout.transitions == []
    clip = layout.clips[0]
    assert clip.record_frame == 0
    assert clip.length_frames == 96
    assert clip.lead_in_frames == 0
    assert clip.lead_out_frames == 0
    assert clip.track_index == LOWER_TRACK
    assert layout.total_frames == 96


def test_clips_overlap_by_transition_duration():
    layout = plan_layout(_project(3, duration=4.0, frames=24))
    assert _slide_lengths(layout) == [96, 96, 96]
    assert _slide_starts(layout) == [0, 72, 144]
    # 3 * 96 - 2 * 24
    assert layout.total_frames == 240


def test_overlapping_slides_split_into_head_and_body():
    layout = plan_layout(_project(3, duration=4.0, frames=24))
    assert [c.segment for c in layout.clips] == [
        SEGMENT_WHOLE,
        SEGMENT_HEAD, SEGMENT_BODY,
        SEGMENT_HEAD, SEGMENT_BODY,
    ]
    assert [c.length_frames for c in layout.clips] == [96, 24, 72, 24, 72]
    assert [c.record_frame for c in layout.clips] == [0, 72, 96, 144, 168]


def test_heads_go_on_the_upper_track_and_bodies_stay_on_v1():
    layout = plan_layout(_project(4))
    for clip in layout.clips:
        expected = UPPER_TRACK if clip.segment == SEGMENT_HEAD else LOWER_TRACK
        assert clip.track_index == expected, clip
    assert layout.track_count == 2


def test_lower_track_is_contiguous():
    layout = plan_layout(_project(5, frames=18))
    lower = [c for c in layout.clips if c.track_index == LOWER_TRACK]
    for previous, following in zip(lower, lower[1:]):
        assert following.record_frame == previous.record_end_frame
    assert lower[0].record_frame == 0
    assert lower[-1].record_end_frame == layout.total_frames


def test_heads_cover_exactly_the_overlap_window():
    layout = plan_layout(_project(4, frames=18))
    for index in range(1, layout.slide_count):
        head = layout.segments_for_index(index)[0]
        outgoing = layout.segments_for_index(index - 1)[-1]
        assert head.segment == SEGMENT_HEAD
        assert head.length_frames == 18
        assert head.record_frame == outgoing.record_end_frame - 18
        assert head.record_end_frame == outgoing.record_end_frame


def test_all_cuts_stay_on_one_track():
    layout = plan_layout(_project(4, transition="none", frames=0))
    assert [c.track_index for c in layout.clips] == [LOWER_TRACK] * 4
    assert [c.segment for c in layout.clips] == [SEGMENT_WHOLE] * 4
    assert layout.track_count == 1
    assert [c.record_frame for c in layout.clips] == [0, 96, 192, 288]
    assert layout.total_frames == 384


def test_lead_in_and_lead_out_mirror_neighbours():
    layout = plan_layout(_project(3, frames=24))
    assert _slide_leads(layout, 0) == (0, 24)
    assert _slide_leads(layout, 1) == (24, 24)
    assert _slide_leads(layout, 2) == (24, 0)
    # No single segment ever carries both sides.
    for clip in layout.clips:
        assert not (clip.lead_in_frames and clip.lead_out_frames)
    body = layout.segments_for_index(1)[-1]
    assert body.lead_out_start_frame == 72 - 24


def test_record_end_frame_and_total_seconds():
    layout = plan_layout(_project(2, frames=12), fps=24.0)
    assert layout.clips[0].record_end_frame == 96
    assert layout.total_frames == 96 + 96 - 12
    assert layout.total_seconds == pytest.approx((96 + 96 - 12) / 24.0)


def test_clip_for_index_round_trips():
    layout = plan_layout(_project(3))
    assert layout.clip_for_index(2).index == 2
    with pytest.raises(KeyError):
        layout.clip_for_index(9)


def test_source_range_is_inclusive():
    layout = plan_layout(_project(1, duration=2.0), fps=30.0)
    clip = layout.clips[0]
    assert clip.source_start_frame == 0
    assert clip.source_end_frame == clip.length_frames - 1 == 59


# --------------------------------------------------------------------------- #
# Per-item overrides
# --------------------------------------------------------------------------- #

def test_per_item_durations_and_transitions_are_honoured():
    proj = SlideshowProject(
        items=[
            MediaItem(
                path="a.jpg",
                duration_seconds=2.0,
                outgoing_transition=TransitionChoice(kind="fade", duration_frames=6),
            ),
            MediaItem(path="b.jpg", duration_seconds=3.0),
            MediaItem(path="c.jpg", duration_seconds=1.0),
        ],
        default_item_duration_seconds=5.0,
        default_transition=TransitionChoice(kind="dissolve", duration_frames=12),
    )
    layout = plan_layout(proj, fps=24.0)
    assert _slide_lengths(layout) == [48, 72, 24]
    assert [t.kind for t in layout.transitions] == ["fade", "dissolve"]
    assert [t.duration_frames for t in layout.transitions] == [6, 12]
    assert _slide_starts(layout) == [0, 42, 102]


def test_last_items_outgoing_transition_is_ignored():
    proj = SlideshowProject(
        items=[
            MediaItem(path="a.jpg"),
            MediaItem(
                path="b.jpg",
                outgoing_transition=TransitionChoice(kind="flip", duration_frames=30),
            ),
        ],
        default_transition=TransitionChoice(kind="none", duration_frames=0),
    )
    layout = plan_layout(proj)
    assert len(layout.transitions) == 1
    assert layout.transitions[0].kind == "none"
    assert layout.clips[-1].lead_out_frames == 0


# --------------------------------------------------------------------------- #
# Clamping
# --------------------------------------------------------------------------- #

def test_overlap_clamped_to_shortest_neighbour():
    proj = SlideshowProject(
        items=[
            MediaItem(path="a.jpg", duration_seconds=4.0),
            MediaItem(path="b.jpg", duration_seconds=10.0 / 24.0),  # 10 frames
        ],
        default_transition=TransitionChoice(kind="dissolve", duration_frames=48),
    )
    layout = plan_layout(proj, fps=24.0)
    assert layout.transitions[0].duration_frames == 10 - MIN_VISIBLE_FRAMES
    assert layout.clips[1].lead_in_frames == 9


def test_two_overlaps_cannot_exceed_the_clip_between_them():
    proj = SlideshowProject(
        items=[
            MediaItem(path="a.jpg", duration_seconds=4.0),
            MediaItem(path="b.jpg", duration_seconds=20.0 / 24.0),  # 20 frames
            MediaItem(path="c.jpg", duration_seconds=4.0),
        ],
        default_transition=TransitionChoice(kind="dissolve", duration_frames=18),
    )
    layout = plan_layout(proj, fps=24.0)
    lead_in, lead_out = _slide_leads(layout, 1)
    assert lead_in + lead_out <= 20 - MIN_VISIBLE_FRAMES
    # Symmetric request stays symmetric after clamping (±1 for an odd budget).
    assert abs(lead_in - lead_out) <= 1
    assert lead_in + lead_out == 20 - MIN_VISIBLE_FRAMES


def test_every_clip_keeps_visible_frames_under_pressure():
    proj = SlideshowProject(
        items=[
            MediaItem(path="{0}.jpg".format(i), duration_seconds=n / 24.0)
            for i, n in enumerate([12, 4, 30, 3, 50])
        ],
        default_transition=TransitionChoice(kind="dissolve", duration_frames=40),
    )
    layout = plan_layout(proj, fps=24.0)
    lengths = _slide_lengths(layout)
    for index, length in enumerate(lengths):
        lead_in, lead_out = _slide_leads(layout, index)
        assert lead_in >= 0 and lead_out >= 0
        assert length - lead_in - lead_out >= MIN_VISIBLE_FRAMES, index
    # Every segment must be at least one frame long, or Resolve rejects it.
    for clip in layout.clips:
        assert clip.length_frames >= 1, clip
    # Record frames must be non-decreasing in placement order.
    frames = [c.record_frame for c in layout.clips]
    assert frames == sorted(frames)


def test_clamped_transition_keeps_kind_and_params():
    proj = SlideshowProject(
        items=[
            MediaItem(path="a.jpg", duration_seconds=5.0 / 24.0),
            MediaItem(path="b.jpg", duration_seconds=5.0 / 24.0),
        ],
        default_transition=TransitionChoice(
            kind="dip_to_color", duration_frames=48, params={"color": "#ff0000"}
        ),
    )
    layout = plan_layout(proj, fps=24.0)
    trans = layout.transitions[0]
    assert trans.kind == "dip_to_color"
    assert trans.params == {"color": "#ff0000"}
    assert trans.duration_frames == 4


def test_unclamped_transition_object_is_reused():
    proj = _project(2, frames=6)
    layout = plan_layout(proj)
    assert layout.transitions[0] is proj.default_transition


def test_a_cut_with_a_duration_still_gets_no_overlap():
    """``kind="none"`` wins over any duration it happens to carry.

    Honouring the duration would carve out an overlap window that nothing
    animates in: a hard cut placed early, with the slide silently losing
    that much screen time.
    """
    proj = _project(3, transition="none", frames=24)
    layout = plan_layout(proj)
    assert [t.duration_frames for t in layout.transitions] == [0, 0]
    assert [c.segment for c in layout.clips] == [SEGMENT_WHOLE] * 3
    assert [c.record_frame for c in layout.clips] == [0, 96, 192]
    assert layout.track_count == 1


def test_a_cut_among_real_transitions_breaks_the_upper_track():
    proj = _project(4, frames=24)
    proj.items[1].outgoing_transition = TransitionChoice(
        kind="none", duration_frames=24
    )
    layout = plan_layout(proj)
    # Slide 2 has no incoming transition, so it stays whole on V1.
    assert [c.segment for c in layout.segments_for_index(2)] == [SEGMENT_WHOLE]
    assert [c.segment for c in layout.segments_for_index(3)] == [
        SEGMENT_HEAD, SEGMENT_BODY
    ]
    lower = [c for c in layout.clips if c.track_index == LOWER_TRACK]
    for previous, following in zip(lower, lower[1:]):
        assert following.record_frame == previous.record_end_frame


# --------------------------------------------------------------------------- #
# Source length limits
# --------------------------------------------------------------------------- #

def test_source_frames_cap_clip_length():
    proj = _project(2, duration=4.0, frames=12)
    layout = plan_layout(proj, fps=24.0, source_frames=[30, None])
    assert _slide_lengths(layout) == [30, 96]
    assert layout.clips[0].source_end_frame == 29


def test_source_frames_zero_or_none_means_unbounded():
    proj = _project(2, duration=4.0, frames=0, transition="none")
    layout = plan_layout(proj, fps=24.0, source_frames=[0, None])
    assert _slide_lengths(layout) == [96, 96]


def test_source_frames_length_mismatch_raises():
    with pytest.raises(ValueError, match="source_frames"):
        plan_layout(_project(3), source_frames=[10, 10])


# --------------------------------------------------------------------------- #
# clipInfo emission
# --------------------------------------------------------------------------- #

def test_to_clip_info_shape():
    clip = PlacedClip(
        index=1,
        track_index=2,
        record_frame=72,
        length_frames=96,
        source_start_frame=0,
        source_end_frame=95,
        lead_in_frames=24,
    )
    sentinel = object()
    info = clip.to_clip_info(sentinel)
    # No startFrame/endFrame: stills ignore them, so length is expressed as a
    # mark in/out on the MediaPoolItem instead.
    assert info == {
        "mediaPoolItem": sentinel,
        "trackIndex": 2,
        "recordFrame": 72,
        "mediaType": 1,
    }


def test_to_clip_info_applies_the_timeline_start_offset():
    clip = PlacedClip(index=0, track_index=1, record_frame=72, length_frames=96)
    assert clip.to_clip_info(object(), record_offset=86400)["recordFrame"] == 86472


def test_empty_layout_properties():
    layout = TimelineLayout()
    assert layout.track_count == 1
    assert layout.total_frames == 0
    assert layout.total_seconds == 0.0


# ------------------------------------------------------------------ #
# Outgoing on top (TAIL segments)
# ------------------------------------------------------------------ #


def _outgoing_project(kinds, *, duration=2.0, frames=12):
    """One slide per kind plus a trailing slide, each with the given kind out."""
    items = [
        MediaItem(path="s{0}.jpg".format(i), duration_seconds=duration)
        for i in range(len(kinds) + 1)
    ]
    for item, kind in zip(items, kinds):
        item.outgoing_transition = TransitionChoice(
            kind=kind, duration_frames=frames
        )
    return SlideshowProject(name="T", items=items)


def _by_kind(*wanted):
    """Predicate: outgoing goes on top for exactly these transition kinds."""
    return lambda choice: choice.kind in wanted


def _assert_tracks_are_sane(layout):
    """V1 is gapless and covers the timeline; V2 segments never collide."""
    lower = sorted(
        (c for c in layout.clips if c.track_index == LOWER_TRACK),
        key=lambda c: c.record_frame,
    )
    at = 0
    for clip in lower:
        assert clip.record_frame == at
        at += clip.length_frames
    assert at == layout.total_frames

    upper = sorted(
        (c for c in layout.clips if c.track_index == UPPER_TRACK),
        key=lambda c: c.record_frame,
    )
    for first, second in zip(upper, upper[1:]):
        assert first.record_frame + first.length_frames <= second.record_frame

    for clip in layout.clips:
        assert clip.length_frames >= 1


def test_outgoing_on_top_carves_a_tail_instead_of_a_head():
    layout = plan_layout(
        _outgoing_project(["page_turn_away"]),
        fps=24.0,
        prefers_outgoing_on_top=_by_kind("page_turn_away"),
    )
    body, tail = layout.segments_for_index(0)
    assert (body.segment, body.track_index) == (SEGMENT_BODY, LOWER_TRACK)
    assert (tail.segment, tail.track_index) == (SEGMENT_TAIL, UPPER_TRACK)
    assert tail.length_frames == 12
    assert tail.lead_out_frames == 12
    # The incoming slide is not split at all — it just sits on V1 underneath.
    (whole,) = layout.segments_for_index(1)
    assert (whole.segment, whole.track_index) == (SEGMENT_WHOLE, LOWER_TRACK)
    assert whole.lead_in_frames == 12


def test_tail_starts_where_the_next_slide_does():
    layout = plan_layout(
        _outgoing_project(["page_turn_away"]),
        fps=24.0,
        prefers_outgoing_on_top=_by_kind("page_turn_away"),
    )
    tail = layout.segments_for_index(0)[-1]
    following = layout.segments_for_index(1)[0]
    assert tail.record_frame == following.record_frame


def test_default_predicate_keeps_the_incoming_on_top_layout():
    plain = plan_layout(_outgoing_project(["page_turn_away"] * 3), fps=24.0)
    assert [c.segment for c in plain.clips].count(SEGMENT_TAIL) == 0
    assert [c.segment for c in plain.clips].count(SEGMENT_HEAD) == 3


def test_tracks_stay_sane_for_every_mix_of_sides():
    mixes = (
        ["dissolve"] * 4,
        ["page_turn_away"] * 4,
        ["dissolve", "page_turn_away", "dissolve", "page_turn_away"],
        ["page_turn_away", "dissolve", "page_turn_away", "dissolve"],
    )
    for kinds in mixes:
        layout = plan_layout(
            _outgoing_project(kinds),
            fps=24.0,
            prefers_outgoing_on_top=_by_kind("page_turn_away"),
        )
        _assert_tracks_are_sane(layout)


def test_total_length_does_not_depend_on_which_side_is_lifted():
    kinds = ["dissolve", "page_turn_away", "dissolve"]
    plain = plan_layout(_outgoing_project(kinds), fps=24.0)
    lifted = plan_layout(
        _outgoing_project(kinds),
        fps=24.0,
        prefers_outgoing_on_top=_by_kind("page_turn_away"),
    )
    assert lifted.total_frames == plain.total_frames
    assert _slide_starts(lifted) == _slide_starts(plain)
    assert _slide_lengths(lifted) == _slide_lengths(plain)


def test_a_slide_can_be_split_at_both_ends():
    layout = plan_layout(
        _outgoing_project(["dissolve", "page_turn_away"]),
        fps=24.0,
        prefers_outgoing_on_top=_by_kind("page_turn_away"),
    )
    head, body, tail = layout.segments_for_index(1)
    assert [head.segment, body.segment, tail.segment] == [
        SEGMENT_HEAD,
        SEGMENT_BODY,
        SEGMENT_TAIL,
    ]
    assert [head.track_index, body.track_index, tail.track_index] == [
        UPPER_TRACK,
        LOWER_TRACK,
        UPPER_TRACK,
    ]
    # The body is what is left after both overlaps are carved off, and it
    # keeps no lead of its own — both went to the lifted segments.
    assert body.length_frames == 48 - 12 - 12
    assert (body.lead_in_frames, body.lead_out_frames) == (0, 0)


def test_a_body_between_two_lifted_neighbours_carries_both_leads():
    # Slide 1 is lifted out of on its left and into on its right, so its own
    # V1 segment is the un-lifted half of both boundaries.
    layout = plan_layout(
        _outgoing_project(["page_turn_away", "dissolve"]),
        fps=24.0,
        prefers_outgoing_on_top=_by_kind("page_turn_away"),
    )
    (middle,) = layout.segments_for_index(1)
    assert middle.segment == SEGMENT_WHOLE
    assert (middle.lead_in_frames, middle.lead_out_frames) == (12, 12)


def test_clamped_overlap_still_leaves_a_visible_tail_body():
    # A short slide whose overlaps are clamped must not lose its V1 segment.
    project = SlideshowProject(
        name="T",
        items=[
            MediaItem(path="a.jpg", duration_seconds=4.0),
            MediaItem(path="b.jpg", duration_seconds=20.0 / 24.0),
            MediaItem(path="c.jpg", duration_seconds=4.0),
        ],
        default_transition=TransitionChoice(
            kind="page_turn_away", duration_frames=18
        ),
    )
    layout = plan_layout(
        project, fps=24.0, prefers_outgoing_on_top=_by_kind("page_turn_away")
    )
    _assert_tracks_are_sane(layout)
    bodies = [c for c in layout.segments_for_index(1) if c.track_index == LOWER_TRACK]
    assert len(bodies) == 1 and bodies[0].length_frames >= MIN_VISIBLE_FRAMES
