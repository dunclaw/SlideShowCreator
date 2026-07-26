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
    lengths = [c.length_frames for c in layout.clips]
    assert lengths == [96, 96, 96]
    assert [c.record_frame for c in layout.clips] == [0, 72, 144]
    # 3 * 96 - 2 * 24
    assert layout.total_frames == 240


def test_tracks_alternate_when_overlapping():
    layout = plan_layout(_project(4))
    assert [c.track_index for c in layout.clips] == [
        LOWER_TRACK, UPPER_TRACK, LOWER_TRACK, UPPER_TRACK
    ]
    assert layout.track_count == 2


def test_all_cuts_stay_on_one_track():
    layout = plan_layout(_project(4, transition="none", frames=0))
    assert [c.track_index for c in layout.clips] == [LOWER_TRACK] * 4
    assert layout.track_count == 1
    assert [c.record_frame for c in layout.clips] == [0, 96, 192, 288]
    assert layout.total_frames == 384


def test_lead_in_and_lead_out_mirror_neighbours():
    layout = plan_layout(_project(3, frames=24))
    first, middle, last = layout.clips
    assert (first.lead_in_frames, first.lead_out_frames) == (0, 24)
    assert (middle.lead_in_frames, middle.lead_out_frames) == (24, 24)
    assert (last.lead_in_frames, last.lead_out_frames) == (24, 0)
    assert middle.lead_out_start_frame == 96 - 24
    assert middle.visible_frames == 96 - 48


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
    assert [c.length_frames for c in layout.clips] == [48, 72, 24]
    assert [t.kind for t in layout.transitions] == ["fade", "dissolve"]
    assert [t.duration_frames for t in layout.transitions] == [6, 12]
    assert [c.record_frame for c in layout.clips] == [0, 42, 102]


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
    middle = layout.clips[1]
    assert middle.lead_in_frames + middle.lead_out_frames <= 20 - MIN_VISIBLE_FRAMES
    assert middle.visible_frames >= MIN_VISIBLE_FRAMES
    # Symmetric request stays symmetric after clamping (±1 for an odd budget).
    assert abs(middle.lead_in_frames - middle.lead_out_frames) <= 1
    assert middle.lead_in_frames + middle.lead_out_frames == 20 - MIN_VISIBLE_FRAMES


def test_every_clip_keeps_visible_frames_under_pressure():
    proj = SlideshowProject(
        items=[
            MediaItem(path="{0}.jpg".format(i), duration_seconds=n / 24.0)
            for i, n in enumerate([12, 4, 30, 3, 50])
        ],
        default_transition=TransitionChoice(kind="dissolve", duration_frames=40),
    )
    layout = plan_layout(proj, fps=24.0)
    for clip in layout.clips:
        assert clip.visible_frames >= MIN_VISIBLE_FRAMES, clip
        assert clip.lead_in_frames >= 0 and clip.lead_out_frames >= 0
    # Record frames must be strictly increasing and non-overlapping per track.
    frames = [c.record_frame for c in layout.clips]
    assert frames == sorted(frames)
    assert len(set(frames)) == len(frames)


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
    assert [c.length_frames for c in layout.clips] == [30, 96]
    assert layout.clips[0].source_end_frame == 29


def test_source_frames_zero_or_none_means_unbounded():
    proj = _project(2, duration=4.0, frames=0, transition="none")
    layout = plan_layout(proj, fps=24.0, source_frames=[0, None])
    assert [c.length_frames for c in layout.clips] == [96, 96]


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
    assert info == {
        "mediaPoolItem": sentinel,
        "startFrame": 0,
        "endFrame": 95,
        "trackIndex": 2,
        "recordFrame": 72,
        "mediaType": 1,
    }


def test_empty_layout_properties():
    layout = TimelineLayout()
    assert layout.track_count == 1
    assert layout.total_frames == 0
    assert layout.total_seconds == 0.0
