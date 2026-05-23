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
from slideshow.project_model import MediaItem, SlideshowProject
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
    storage = MagicMock()
    pool = MagicMock()

    # Resolve will return items in arbitrary order — simulate that.
    item_a = MagicMock()
    item_a.GetClipProperty.return_value = "a.jpg"
    item_b = MagicMock()
    item_b.GetClipProperty.return_value = "b.jpg"
    item_c = MagicMock()
    item_c.GetClipProperty.return_value = "c.jpg"

    storage.AddItemListToMediaPool.return_value = [item_b, item_c, item_a]

    paths = ["/abs/a.jpg", "/abs/b.jpg", "/abs/c.jpg"]
    result = tb._import_media(storage, pool, paths)

    assert result == [item_a, item_b, item_c]


def test_import_media_reports_missing_as_none():
    storage = MagicMock()
    pool = MagicMock()
    found = MagicMock()
    found.GetClipProperty.return_value = "present.jpg"
    storage.AddItemListToMediaPool.return_value = [found]

    result = tb._import_media(
        storage, pool, ["/abs/present.jpg", "/abs/missing.jpg"]
    )
    assert result == [found, None]


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
    media_pool.CreateTimelineFromClips.return_value = timeline

    media_storage = MagicMock()

    ctx.resolve.GetProjectManager.return_value.GetCurrentProject.return_value = project
    project.GetMediaPool.return_value = media_pool
    ctx.resolve.GetMediaStorage.return_value = media_storage

    return ctx, project, media_pool, media_storage, new_folder, timeline


def test_builder_creates_subfolder_and_imports_media():
    ctx, project, mp, ms, new_folder, timeline = _wire_mock_context()

    items = []
    for i, name in enumerate(["a.jpg", "b.jpg"]):
        m = MagicMock()
        m.GetClipProperty.return_value = name
        items.append(m)
    ms.AddItemListToMediaPool.return_value = items

    proj = SlideshowProject.from_paths(["/abs/a.jpg", "/abs/b.jpg"], name="Demo")

    out = tb.TimelineBuilder(proj, context=ctx).build()

    assert out is timeline
    mp.AddSubFolder.assert_called_once()
    mp.SetCurrentFolder.assert_called_once_with(new_folder)
    ms.AddItemListToMediaPool.assert_called_once()

    args, _ = mp.CreateTimelineFromClips.call_args
    assert args[0] == "Demo"
    clip_infos = args[1]
    assert len(clip_infos) == 2
    assert clip_infos[0]["startFrame"] == 0
    # default duration is 4.0s at 24 fps → 96 frames, endFrame inclusive = 95
    assert clip_infos[0]["endFrame"] == int(4.0 * 24) - 1


def test_builder_raises_when_import_partially_fails():
    ctx, project, mp, ms, _new, _tl = _wire_mock_context()

    found = MagicMock()
    found.GetClipProperty.return_value = "a.jpg"
    ms.AddItemListToMediaPool.return_value = [found]

    proj = SlideshowProject.from_paths(["/abs/a.jpg", "/abs/missing.jpg"])
    with pytest.raises(RuntimeError, match="Failed to import"):
        tb.TimelineBuilder(proj, context=ctx).build()


def test_builder_raises_on_empty_project():
    ctx, *_ = _wire_mock_context()
    with pytest.raises(ValueError, match="no items"):
        tb.TimelineBuilder(SlideshowProject(), context=ctx).build()


def test_builder_raises_when_resolve_returns_no_timeline():
    ctx, project, mp, ms, _new, _tl = _wire_mock_context()
    mp.CreateTimelineFromClips.return_value = None

    found = MagicMock()
    found.GetClipProperty.return_value = "a.jpg"
    ms.AddItemListToMediaPool.return_value = [found]

    proj = SlideshowProject.from_paths(["/abs/a.jpg"])
    with pytest.raises(RuntimeError, match="returned None"):
        tb.TimelineBuilder(proj, context=ctx).build()


def test_builder_reuses_existing_subfolder():
    ctx, project, mp, ms, _new, _tl = _wire_mock_context()

    existing = MagicMock()
    existing.GetName.return_value = "SlideShowCreator"
    mp.GetRootFolder.return_value.GetSubFolderList.return_value = [existing]

    found = MagicMock()
    found.GetClipProperty.return_value = "a.jpg"
    ms.AddItemListToMediaPool.return_value = [found]

    proj = SlideshowProject.from_paths(["/abs/a.jpg"])
    tb.TimelineBuilder(proj, context=ctx).build()

    mp.AddSubFolder.assert_not_called()
    mp.SetCurrentFolder.assert_called_once_with(existing)


def test_per_item_duration_overrides_default():
    ctx, project, mp, ms, _new, _tl = _wire_mock_context(fps="30")

    items = []
    for name in ["a.jpg", "b.jpg"]:
        m = MagicMock()
        m.GetClipProperty.return_value = name
        items.append(m)
    ms.AddItemListToMediaPool.return_value = items

    proj = SlideshowProject(
        name="X",
        items=[
            MediaItem(path="/abs/a.jpg", duration_seconds=2.0),
            MediaItem(path="/abs/b.jpg"),  # falls back to default
        ],
        default_item_duration_seconds=3.0,
    )
    tb.TimelineBuilder(proj, context=ctx).build()

    clip_infos = mp.CreateTimelineFromClips.call_args[0][1]
    assert clip_infos[0]["endFrame"] == int(2.0 * 30) - 1
    assert clip_infos[1]["endFrame"] == int(3.0 * 30) - 1
