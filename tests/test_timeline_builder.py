"""Unit tests for project_model + timeline_builder (no Resolve required).

The Resolve API surface is mocked. The integration test that actually drives
a running Resolve lives in ``scripts/build_demo_timeline.py``.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from slideshow import timeline_builder as tb
from slideshow.project_model import MediaItem, SlideshowProject, TransitionChoice
from slideshow.resolve_bridge import ResolveContext


# --------------------------------------------------------------------------- #
# project_model
# --------------------------------------------------------------------------- #

def test_slideshow_project_from_paths_creates_items():
    proj = SlideshowProject.from_paths(["a.jpg", "b.jpg", "c.jpg"])
    assert [i.path for i in proj.items] == ["a.jpg", "b.jpg", "c.jpg"]
    assert proj.default_item_duration_seconds == 4.0


def test_effective_duration_prefers_item_override():
    proj = SlideshowProject(default_item_duration_seconds=4.0)
    item_default = MediaItem(path="x.jpg")
    item_custom = MediaItem(path="y.jpg", duration_seconds=7.5)
    item_zero = MediaItem(path="z.jpg", duration_seconds=0)
    assert proj.effective_duration(item_default) == 4.0
    assert proj.effective_duration(item_custom) == 7.5
    assert proj.effective_duration(item_zero) == 4.0  # 0 → fall back to default


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "seconds, fps, expected",
    [
        (1.0, 24.0, 24),
        (0.5, 24.0, 12),
        (4.0, 30.0, 120),
        (0.0, 24.0, 1),    # clamps to at least 1 frame
        (-1.0, 24.0, 1),
        (1.0 / 24.0, 24.0, 1),  # one-frame slide
        (0.999999, 24.0, 24),   # rounding
    ],
)
def test_seconds_to_frames(seconds, fps, expected):
    assert tb._seconds_to_frames(seconds, fps) == expected


def test_timeline_fps_uses_project_setting():
    proj = MagicMock()
    proj.GetSetting.return_value = "29.97"
    assert tb._timeline_fps(proj) == pytest.approx(29.97)
    proj.GetSetting.assert_called_once_with("timelineFrameRate")


def test_timeline_fps_falls_back_when_setting_invalid():
    proj = MagicMock()
    proj.GetSetting.return_value = None
    assert tb._timeline_fps(proj, fallback=24.0) == 24.0
    proj.GetSetting.return_value = "bogus"
    assert tb._timeline_fps(proj, fallback=25.0) == 25.0


def _fake_pool(available=(), existing=()):
    """A mocked MediaPool whose ImportMedia handles one path per call.

    ``available`` are paths Resolve can import; ``existing`` are paths already
    sitting in the pool (which ``ImportMedia`` refuses to import again).
    """
    pool = MagicMock()

    def _clip(path):
        m = MagicMock()
        m.GetClipProperty.side_effect = lambda prop: (
            path if prop == "File Path" else os.path.basename(path)
        )
        return m

    existing_clips = [_clip(p) for p in existing]
    root = MagicMock()
    root.GetClipList.return_value = existing_clips
    root.GetSubFolderList.return_value = []
    pool.GetRootFolder.return_value = root

    made = {p: _clip(p) for p in available}

    def _import(paths):
        assert len(paths) == 1, (
            "ImportMedia must be called with exactly one path per call, "
            "or Resolve collapses consecutive stills into an image sequence"
        )
        clip = made.get(paths[0])
        return [clip] if clip is not None else []

    pool.ImportMedia.side_effect = _import
    return pool


def test_import_media_preserves_input_order():
    paths = ["D:/abs/a.jpg", "D:/abs/b.jpg", "D:/abs/c.jpg"]
    pool = _fake_pool(available=paths)

    result = tb._import_media(pool, [r"D:\abs\a.jpg", r"D:\abs\b.jpg", r"D:\abs\c.jpg"])

    assert [r.GetClipProperty("File Path") for r in result] == paths
    for call in pool.ImportMedia.call_args_list:
        assert all("\\" not in p for p in call[0][0])


def test_import_media_imports_one_path_per_call():
    """Batching several consecutive stills makes Resolve build an image sequence."""
    paths = ["D:/abs/DSCF0043.JPG", "D:/abs/DSCF0044.JPG", "D:/abs/DSCF0045.JPG"]
    pool = _fake_pool(available=paths)

    tb._import_media(pool, paths)

    assert pool.ImportMedia.call_count == 3
    assert [c[0][0] for c in pool.ImportMedia.call_args_list] == [[p] for p in paths]


def test_import_media_reuses_clips_already_in_the_pool():
    pool = _fake_pool(available=["D:/abs/new.jpg"], existing=["D:/abs/old.jpg"])

    result = tb._import_media(pool, [r"D:\abs\old.jpg", r"D:\abs\new.jpg"])

    assert all(r is not None for r in result)
    # The pre-existing clip must not be re-imported.
    assert pool.ImportMedia.call_count == 1
    assert pool.ImportMedia.call_args[0][0] == ["D:/abs/new.jpg"]


def test_import_media_reports_missing_as_none():
    pool = _fake_pool(available=["D:/abs/present.jpg"])

    result = tb._import_media(pool, [r"D:\abs\present.jpg", r"D:\abs\missing.jpg"])

    assert result[0] is not None
    assert result[1] is None


def test_normalize_for_resolve_uses_forward_slashes():
    out = tb._normalize_for_resolve(r"C:\foo\bar\baz.jpg")
    assert "\\" not in out
    assert out.endswith("/foo/bar/baz.jpg")


# --------------------------------------------------------------------------- #
# TimelineBuilder
# --------------------------------------------------------------------------- #

def _wire_mock_context(*, fps="24"):
    """Build a mocked ResolveContext with the call graph the builder needs."""
    ctx = ResolveContext(resolve=MagicMock())

    project = MagicMock()
    project.GetSetting.return_value = fps

    media_pool = MagicMock()
    root_folder = MagicMock()
    root_folder.GetSubFolderList.return_value = []
    root_folder.GetClipList.return_value = []
    media_pool.GetRootFolder.return_value = root_folder

    new_folder = MagicMock()
    media_pool.AddSubFolder.return_value = new_folder

    timeline = MagicMock()
    timeline.GetName.return_value = "Slideshow"
    timeline.GetTrackCount.return_value = 1
    timeline.GetStartFrame.return_value = 0
    media_pool.CreateTimelineFromClips.return_value = timeline
    media_pool.CreateEmptyTimeline.return_value = timeline
    media_pool.AppendToTimeline.side_effect = lambda infos: [
        MagicMock(name="timeline-item") for _ in infos
    ]

    ctx.resolve.GetProjectManager.return_value.GetCurrentProject.return_value = project
    project.GetMediaPool.return_value = media_pool

    return ctx, project, media_pool, new_folder, timeline


def _mock_media(names):
    """Wire a pool's ImportMedia to serve one clip per call, in order."""
    items = []
    for name in names:
        m = MagicMock()
        m.GetClipProperty.return_value = name
        items.append(m)
    return items


def _serve_one_at_a_time(pool, items):
    queue = list(items)
    pool._served = list(items)
    pool.ImportMedia.side_effect = lambda paths: [queue.pop(0)] if queue else []


def test_builder_creates_subfolder_and_imports_media():
    ctx, project, mp, new_folder, timeline = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["a.jpg", "b.jpg"]))

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg", r"D:\abs\b.jpg"], name="Demo")

    out = tb.TimelineBuilder(proj, context=ctx, overlap=False).build()

    assert out.timeline is timeline
    mp.AddSubFolder.assert_called_once()
    mp.SetCurrentFolder.assert_called_once_with(new_folder)
    # One call per path, never batched (image-sequence auto-detection).
    assert mp.ImportMedia.call_count == 2
    for call in mp.ImportMedia.call_args_list:
        paths = call[0][0]
        assert len(paths) == 1
        assert "\\" not in paths[0]

    args, _ = mp.CreateTimelineFromClips.call_args
    assert args[0] == "Demo"
    clip_infos = args[1]
    assert len(clip_infos) == 2
    assert clip_infos[0]["mediaType"] == 1
    # default duration is 4.0s at 24 fps → 96 frames, mark out inclusive = 95
    assert mp._served[0].SetMarkInOut.call_args[0] == (0, int(4.0 * 24) - 1, "video")


def test_builder_raises_when_import_partially_fails():
    ctx, project, mp, _new, _tl = _wire_mock_context()

    found = MagicMock()
    found.GetClipProperty.return_value = "a.jpg"
    _serve_one_at_a_time(mp, [found])

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg", r"D:\abs\missing.jpg"])
    with pytest.raises(RuntimeError, match="Failed to import"):
        tb.TimelineBuilder(proj, context=ctx).build()


def test_builder_raises_on_empty_project():
    ctx, *_ = _wire_mock_context()
    with pytest.raises(ValueError, match="no items"):
        tb.TimelineBuilder(SlideshowProject(), context=ctx).build()


def test_builder_raises_when_resolve_returns_no_timeline():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.CreateTimelineFromClips.return_value = None
    _serve_one_at_a_time(mp, _mock_media(["a.jpg"]))

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg"])
    with pytest.raises(RuntimeError, match="returned None"):
        tb.TimelineBuilder(proj, context=ctx, overlap=False).build()


def test_builder_raises_when_empty_timeline_creation_fails():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.CreateEmptyTimeline.return_value = None
    _serve_one_at_a_time(mp, _mock_media(["a.jpg"]))

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg"])
    with pytest.raises(RuntimeError, match="CreateEmptyTimeline"):
        tb.TimelineBuilder(proj, context=ctx).build()


def test_builder_reuses_existing_subfolder():
    ctx, project, mp, _new, _tl = _wire_mock_context()

    existing = MagicMock()
    existing.GetName.return_value = "SlideShowCreator"
    mp.GetRootFolder.return_value.GetSubFolderList.return_value = [existing]
    _serve_one_at_a_time(mp, _mock_media(["a.jpg"]))

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg"])
    tb.TimelineBuilder(proj, context=ctx).build()

    mp.AddSubFolder.assert_not_called()
    mp.SetCurrentFolder.assert_called_once_with(existing)


def test_per_item_duration_overrides_default():
    ctx, project, mp, _new, _tl = _wire_mock_context(fps="30")
    _serve_one_at_a_time(mp, _mock_media(["a.jpg", "b.jpg"]))

    proj = SlideshowProject(
        name="X",
        items=[
            MediaItem(path=r"D:\abs\a.jpg", duration_seconds=2.0),
            MediaItem(path=r"D:\abs\b.jpg"),  # falls back to default
        ],
        default_item_duration_seconds=3.0,
    )
    tb.TimelineBuilder(proj, context=ctx, overlap=False).build()

    clip_infos = mp.CreateTimelineFromClips.call_args[0][1]
    assert len(clip_infos) == 2
    # Duration is carried by the mark in/out, not startFrame/endFrame.
    marks = [c.SetMarkInOut.call_args[0] for c in mp._served]
    assert marks == [(0, int(2.0 * 30) - 1, "video"), (0, int(3.0 * 30) - 1, "video")]
    # ...and the marks are cleared again so the user's pool is left alone.
    for clip in mp._served:
        clip.ClearMarkInOut.assert_called_once()


# --------------------------------------------------------------------------- #
# Overlapping layout
# --------------------------------------------------------------------------- #

def _overlap_project(count=3, *, kind="dissolve", frames=24):
    return SlideshowProject(
        name="Demo",
        items=[MediaItem(path="D:\\abs\\s{0}.jpg".format(i)) for i in range(count)],
        default_transition=TransitionChoice(kind=kind, duration_frames=frames),
    )


def test_overlapping_build_splits_slides_across_two_tracks():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(
        ["s0.jpg", "s1.jpg", "s2.jpg"]
    ))

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    project.SetCurrentTimeline.assert_called_once_with(timeline)
    infos = [call.args[0][0] for call in mp.AppendToTimeline.call_args_list]
    # Slide 0 is whole on V1; slides 1 and 2 split into a V2 head (the
    # overlap window) and a V1 body that keeps the lower track contiguous.
    assert [i["trackIndex"] for i in infos] == [1, 2, 1, 2, 1]
    assert [i["recordFrame"] for i in infos] == [0, 72, 96, 144, 168]
    # Length rides on the pool item's mark in/out, not the clipInfo. Each
    # split slide is marked twice — once per segment — hence the last mark
    # on slides 1 and 2 is the body length.
    marks = [c.SetMarkInOut.call_args[0] for c in mp._served]
    assert marks == [(0, 95, "video"), (0, 71, "video"), (0, 71, "video")]
    assert result.layout.total_frames == 240
    assert len(result.timeline_items) == 5


def test_overlapping_build_offsets_record_frames_by_timeline_start():
    """recordFrame is absolute, and Resolve timelines start at 01:00:00:00.

    Without the offset the clips land an hour before the timeline start:
    they exist and the track header counts them, but nothing is visible and
    playback does nothing.
    """
    ctx, project, mp, _new, timeline = _wire_mock_context()
    timeline.GetStartFrame.return_value = 86400
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    infos = [call.args[0][0] for call in mp.AppendToTimeline.call_args_list]
    assert [i["recordFrame"] for i in infos] == [
        86400, 86472, 86496, 86544, 86568
    ]


def test_overlapping_build_tolerates_unreadable_start_frame():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    timeline.GetStartFrame.side_effect = RuntimeError("nope")
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    infos = [call.args[0][0] for call in mp.AppendToTimeline.call_args_list]
    assert [i["recordFrame"] for i in infos] == [0, 72, 96, 144, 168]


def test_overlapping_build_adds_the_second_video_track():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    timeline.GetTrackCount.return_value = 1
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg"]))

    tb.TimelineBuilder(_overlap_project(2), context=ctx).build()

    timeline.AddTrack.assert_called_once_with("video")


def test_no_extra_track_when_every_transition_is_a_cut():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg"]))

    result = tb.TimelineBuilder(
        _overlap_project(2, kind="none", frames=0), context=ctx
    ).build()

    timeline.AddTrack.assert_not_called()
    infos = [call.args[0][0] for call in mp.AppendToTimeline.call_args_list]
    assert [i["trackIndex"] for i in infos] == [1, 1]
    assert result.comps_applied == 0


def test_transitions_are_applied_to_every_touched_clip():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    assert len(result.transition_plans) == 2
    assert all(p is not None for p in result.transition_plans)
    # The split layout always puts the incoming slide on top, so no plan is
    # ever mirrored and a dissolve only ever animates the incoming side.
    # That is exactly one comp per transition, on each slide's V2 head.
    assert result.comps_applied == 2
    for plan in result.transition_plans:
        assert plan.outgoing.is_empty()
        assert plan.incoming.blend == [(0, 0.0), (24, 1.0)]

    segments = [(c.index, c.segment) for c in result.layout.clips]
    for position, (index, segment) in enumerate(segments):
        item = result.timeline_items[position]
        if segment == tb.SEGMENT_HEAD:
            item.LoadFusionCompByName.assert_called()
        else:
            item.LoadFusionCompByName.assert_not_called()


def test_item_for_index_returns_the_head_and_items_for_index_both_halves():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    assert len(result.items_for_index(0)) == 1
    assert len(result.items_for_index(1)) == 2
    assert result.item_for_index(1) is result.timeline_items[1]
    assert result.items_for_index(1)[1] is result.timeline_items[2]


def test_apply_transitions_can_be_disabled():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg"]))

    result = tb.TimelineBuilder(
        _overlap_project(2), context=ctx, apply_transitions=False
    ).build()

    assert result.comps_applied == 0
    assert result.transition_plans == []
    for item in result.timeline_items:
        item.LoadFusionCompByName.assert_not_called()


def test_auto_transitions_fall_back_to_a_concrete_kind():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg"]))

    result = tb.TimelineBuilder(
        _overlap_project(2, kind="auto"), context=ctx
    ).build()

    assert result.transition_plans[0].kind == tb.AUTO_FALLBACK_KIND


def test_build_raises_when_append_places_nothing():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg"]))
    mp.AppendToTimeline.side_effect = None
    mp.AppendToTimeline.return_value = []

    with pytest.raises(RuntimeError, match="AppendToTimeline placed nothing"):
        tb.TimelineBuilder(_overlap_project(2), context=ctx).build()


def test_item_for_index_maps_back_to_project_items():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    assert result.item_for_index(1) is result.timeline_items[1]


def test_build_slideshow_helper_returns_a_build_result():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg"]))

    result = tb.build_slideshow(_overlap_project(1), context=ctx)

    assert isinstance(result, tb.BuildResult)
    assert result.timeline is timeline


# --------------------------------------------------------------------------- #
# Source length handling
# --------------------------------------------------------------------------- #

def _media_with_properties(props):
    item = MagicMock()
    item.GetClipProperty.side_effect = lambda key: props.get(key)
    return item


def test_video_source_length_caps_the_clip():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = [
        _media_with_properties(
            {"File Name": "clip.mov", "Type": "Video", "Frames": "40"}
        )
    ]

    proj = SlideshowProject(
        name="V", items=[MediaItem(path=r"D:\abs\clip.mov")],
        default_item_duration_seconds=4.0,
    )
    result = tb.TimelineBuilder(proj, context=ctx).build()

    assert result.layout.clips[0].length_frames == 40


def test_still_frame_count_is_ignored():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = [
        _media_with_properties(
            {"File Name": "s.jpg", "Type": "Still", "Frames": "1"}
        )
    ]

    proj = SlideshowProject(
        name="S", items=[MediaItem(path=r"D:\abs\s.jpg")],
        default_item_duration_seconds=4.0,
    )
    result = tb.TimelineBuilder(proj, context=ctx).build()

    assert result.layout.clips[0].length_frames == 96


def test_source_length_check_can_be_disabled():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = [
        _media_with_properties(
            {"File Name": "clip.mov", "Type": "Video", "Frames": "40"}
        )
    ]

    proj = SlideshowProject(
        name="V", items=[MediaItem(path=r"D:\abs\clip.mov")],
        default_item_duration_seconds=4.0,
    )
    result = tb.TimelineBuilder(
        proj, context=ctx, respect_source_length=False
    ).build()

    assert result.layout.clips[0].length_frames == 96


def test_unreadable_clip_properties_are_treated_as_unbounded():
    broken = MagicMock()
    broken.GetClipProperty.side_effect = Exception("PyRemoteObject says no")
    assert tb._source_frame_count(broken) is None


# --------------------------------------------------------------------------- #
# Outgoing-on-top transitions
# --------------------------------------------------------------------------- #


def test_outgoing_on_top_transitions_lift_the_departing_slide():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    result = tb.TimelineBuilder(
        _overlap_project(kind="page_turn_away", frames=24), context=ctx
    ).build()

    segments = [(c.index, c.segment, c.track_index) for c in result.layout.clips]
    assert segments == [
        (0, tb.SEGMENT_BODY, 1),
        (0, tb.SEGMENT_TAIL, 2),
        (1, tb.SEGMENT_BODY, 1),
        (1, tb.SEGMENT_TAIL, 2),
        (2, tb.SEGMENT_WHOLE, 1),
    ]


def test_the_lifted_outgoing_clip_is_the_one_that_gets_the_comp():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    result = tb.TimelineBuilder(
        _overlap_project(kind="page_turn_away", frames=24), context=ctx
    ).build()

    # Both plans are mirrored, so the rotation lands on the outgoing half...
    assert result.comps_applied == 2
    for plan in result.transition_plans:
        assert plan.incoming.is_empty()
        assert plan.outgoing.page_turn is not None

    # ...and the only clips carrying a comp are the two V2 tails.
    for position, placed in enumerate(result.layout.clips):
        item = result.timeline_items[position]
        if placed.segment == tb.SEGMENT_TAIL:
            item.LoadFusionCompByName.assert_called()
        else:
            item.LoadFusionCompByName.assert_not_called()


def test_v1_stays_gapless_when_sides_are_mixed():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"]))

    proj = _overlap_project(kind="dissolve", frames=24)
    proj.items[0].outgoing_transition = TransitionChoice(
        kind="page_turn_away", duration_frames=24
    )
    result = tb.TimelineBuilder(proj, context=ctx).build()

    lower = sorted(
        (c for c in result.layout.clips if c.track_index == 1),
        key=lambda c: c.record_frame,
    )
    at = 0
    for clip in lower:
        assert clip.record_frame == at
        at += clip.length_frames
    assert at == result.layout.total_frames
    # Slide 1 is uncovered on its left and covered on its right, so it stays
    # whole while carrying both leads.
    (middle,) = result.layout.segments_for_index(1)
    assert middle.segment == tb.SEGMENT_WHOLE
    assert (middle.lead_in_frames, middle.lead_out_frames) == (24, 24)


def test_auto_is_resolved_before_the_layout_decides_which_side_to_lift():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    _serve_one_at_a_time(mp, _mock_media(["s0.jpg", "s1.jpg"]))

    # AUTO_FALLBACK_KIND is an ordinary incoming-on-top transition, so an
    # auto boundary must produce a head, never a tail.
    result = tb.TimelineBuilder(
        _overlap_project(count=2, kind="auto", frames=24), context=ctx
    ).build()

    assert [c.segment for c in result.layout.clips].count(tb.SEGMENT_TAIL) == 0
    assert [c.segment for c in result.layout.clips].count(tb.SEGMENT_HEAD) == 1
