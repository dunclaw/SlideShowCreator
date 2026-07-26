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


def test_import_media_preserves_input_order():
    pool = MagicMock()

    # Resolve will return items in arbitrary order — simulate that.
    item_a = MagicMock()
    item_a.GetClipProperty.return_value = "a.jpg"
    item_b = MagicMock()
    item_b.GetClipProperty.return_value = "b.jpg"
    item_c = MagicMock()
    item_c.GetClipProperty.return_value = "c.jpg"

    pool.ImportMedia.return_value = [item_b, item_c, item_a]

    paths = [r"D:\abs\a.jpg", r"D:\abs\b.jpg", r"D:\abs\c.jpg"]
    result = tb._import_media(pool, paths)

    assert result == [item_a, item_b, item_c]
    # And confirm we normalized to forward slashes before calling Resolve.
    called_paths = pool.ImportMedia.call_args[0][0]
    assert all("\\" not in p for p in called_paths)


def test_import_media_reports_missing_as_none():
    pool = MagicMock()
    found = MagicMock()
    found.GetClipProperty.return_value = "present.jpg"
    pool.ImportMedia.return_value = [found]

    result = tb._import_media(
        pool, [r"D:\abs\present.jpg", r"D:\abs\missing.jpg"]
    )
    assert result == [found, None]


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
    media_pool.GetRootFolder.return_value = root_folder

    new_folder = MagicMock()
    media_pool.AddSubFolder.return_value = new_folder

    timeline = MagicMock()
    timeline.GetName.return_value = "Slideshow"
    timeline.GetTrackCount.return_value = 1
    media_pool.CreateTimelineFromClips.return_value = timeline
    media_pool.CreateEmptyTimeline.return_value = timeline
    media_pool.AppendToTimeline.side_effect = lambda infos: [
        MagicMock(name="timeline-item") for _ in infos
    ]

    ctx.resolve.GetProjectManager.return_value.GetCurrentProject.return_value = project
    project.GetMediaPool.return_value = media_pool

    return ctx, project, media_pool, new_folder, timeline


def _mock_media(names):
    items = []
    for name in names:
        m = MagicMock()
        m.GetClipProperty.return_value = name
        items.append(m)
    return items


def test_builder_creates_subfolder_and_imports_media():
    ctx, project, mp, new_folder, timeline = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["a.jpg", "b.jpg"])

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg", r"D:\abs\b.jpg"], name="Demo")

    out = tb.TimelineBuilder(proj, context=ctx, overlap=False).build()

    assert out.timeline is timeline
    mp.AddSubFolder.assert_called_once()
    mp.SetCurrentFolder.assert_called_once_with(new_folder)
    mp.ImportMedia.assert_called_once()
    # Confirm the paths passed to ImportMedia are absolute & forward-slashed.
    called = mp.ImportMedia.call_args[0][0]
    assert all("\\" not in p for p in called)

    args, _ = mp.CreateTimelineFromClips.call_args
    assert args[0] == "Demo"
    clip_infos = args[1]
    assert len(clip_infos) == 2
    assert clip_infos[0]["startFrame"] == 0
    # default duration is 4.0s at 24 fps → 96 frames, endFrame inclusive = 95
    assert clip_infos[0]["endFrame"] == int(4.0 * 24) - 1


def test_builder_raises_when_import_partially_fails():
    ctx, project, mp, _new, _tl = _wire_mock_context()

    found = MagicMock()
    found.GetClipProperty.return_value = "a.jpg"
    mp.ImportMedia.return_value = [found]

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
    mp.ImportMedia.return_value = _mock_media(["a.jpg"])

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg"])
    with pytest.raises(RuntimeError, match="returned None"):
        tb.TimelineBuilder(proj, context=ctx, overlap=False).build()


def test_builder_raises_when_empty_timeline_creation_fails():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.CreateEmptyTimeline.return_value = None
    mp.ImportMedia.return_value = _mock_media(["a.jpg"])

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg"])
    with pytest.raises(RuntimeError, match="CreateEmptyTimeline"):
        tb.TimelineBuilder(proj, context=ctx).build()


def test_builder_reuses_existing_subfolder():
    ctx, project, mp, _new, _tl = _wire_mock_context()

    existing = MagicMock()
    existing.GetName.return_value = "SlideShowCreator"
    mp.GetRootFolder.return_value.GetSubFolderList.return_value = [existing]
    mp.ImportMedia.return_value = _mock_media(["a.jpg"])

    proj = SlideshowProject.from_paths([r"D:\abs\a.jpg"])
    tb.TimelineBuilder(proj, context=ctx).build()

    mp.AddSubFolder.assert_not_called()
    mp.SetCurrentFolder.assert_called_once_with(existing)


def test_per_item_duration_overrides_default():
    ctx, project, mp, _new, _tl = _wire_mock_context(fps="30")
    mp.ImportMedia.return_value = _mock_media(["a.jpg", "b.jpg"])

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
    assert clip_infos[0]["endFrame"] == int(2.0 * 30) - 1
    assert clip_infos[1]["endFrame"] == int(3.0 * 30) - 1


# --------------------------------------------------------------------------- #
# Overlapping layout
# --------------------------------------------------------------------------- #

def _overlap_project(count=3, *, kind="dissolve", frames=24):
    return SlideshowProject(
        name="Demo",
        items=[MediaItem(path="D:\\abs\\s{0}.jpg".format(i)) for i in range(count)],
        default_transition=TransitionChoice(kind=kind, duration_frames=frames),
    )


def test_overlapping_build_places_clips_on_alternating_tracks():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(
        ["s0.jpg", "s1.jpg", "s2.jpg"]
    )

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    project.SetCurrentTimeline.assert_called_once_with(timeline)
    infos = [call.args[0][0] for call in mp.AppendToTimeline.call_args_list]
    assert [i["trackIndex"] for i in infos] == [1, 2, 1]
    assert [i["recordFrame"] for i in infos] == [0, 72, 144]
    assert [i["endFrame"] for i in infos] == [95, 95, 95]
    assert result.layout.total_frames == 240
    assert len(result.timeline_items) == 3


def test_overlapping_build_adds_the_second_video_track():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    timeline.GetTrackCount.return_value = 1
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg"])

    tb.TimelineBuilder(_overlap_project(2), context=ctx).build()

    timeline.AddTrack.assert_called_once_with("video")


def test_no_extra_track_when_every_transition_is_a_cut():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg"])

    result = tb.TimelineBuilder(
        _overlap_project(2, kind="none", frames=0), context=ctx
    ).build()

    timeline.AddTrack.assert_not_called()
    infos = [call.args[0][0] for call in mp.AppendToTimeline.call_args_list]
    assert [i["trackIndex"] for i in infos] == [1, 1]
    assert result.comps_applied == 0


def test_transitions_are_applied_to_every_touched_clip():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"])

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    assert len(result.transition_plans) == 2
    assert all(p is not None for p in result.transition_plans)
    # A dissolve only animates its incoming half — the outgoing clip stays
    # opaque underneath — so the first slide needs no comp at all.
    assert result.comps_applied == 2
    result.timeline_items[0].LoadFusionCompByName.assert_not_called()
    for item in result.timeline_items[1:]:
        item.LoadFusionCompByName.assert_called()


def test_apply_transitions_can_be_disabled():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg"])

    result = tb.TimelineBuilder(
        _overlap_project(2), context=ctx, apply_transitions=False
    ).build()

    assert result.comps_applied == 0
    assert result.transition_plans == []
    for item in result.timeline_items:
        item.LoadFusionCompByName.assert_not_called()


def test_auto_transitions_fall_back_to_a_concrete_kind():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg"])

    result = tb.TimelineBuilder(
        _overlap_project(2, kind="auto"), context=ctx
    ).build()

    assert result.transition_plans[0].kind == tb.AUTO_FALLBACK_KIND


def test_build_raises_when_append_places_nothing():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg"])
    mp.AppendToTimeline.side_effect = None
    mp.AppendToTimeline.return_value = []

    with pytest.raises(RuntimeError, match="AppendToTimeline placed nothing"):
        tb.TimelineBuilder(_overlap_project(2), context=ctx).build()


def test_item_for_index_maps_back_to_project_items():
    ctx, project, mp, _new, _tl = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg", "s1.jpg", "s2.jpg"])

    result = tb.TimelineBuilder(_overlap_project(), context=ctx).build()

    assert result.item_for_index(1) is result.timeline_items[1]


def test_build_slideshow_helper_returns_a_build_result():
    ctx, project, mp, _new, timeline = _wire_mock_context()
    mp.ImportMedia.return_value = _mock_media(["s0.jpg"])

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
