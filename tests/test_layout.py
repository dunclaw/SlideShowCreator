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
