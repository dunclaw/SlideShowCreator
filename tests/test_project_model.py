"""Tests for src/slideshow/project_model.py."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from slideshow.project_model import (
    DEFAULT_TRANSITION_DURATION_FRAMES,
    PROJECT_SCHEMA_VERSION,
    AudioSettings,
    MediaItem,
    SlideshowProject,
    TitleSpec,
    TransitionChoice,
)


# --------------------------------------------------------------------------- #
# TransitionChoice
# --------------------------------------------------------------------------- #

class TestTransitionChoice:
    def test_defaults(self):
        t = TransitionChoice()
        assert t.kind == "dissolve"
        assert t.duration_frames == DEFAULT_TRANSITION_DURATION_FRAMES
        assert t.params == {}
        assert not t.is_cut()

    def test_is_cut_for_none_kind(self):
        assert TransitionChoice(kind="none").is_cut()

    def test_is_cut_for_zero_frames(self):
        assert TransitionChoice(kind="dissolve", duration_frames=0).is_cut()

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError, match="Unknown transition kind"):
            TransitionChoice(kind="warp_speed")

    def test_rejects_negative_duration(self):
        with pytest.raises(ValueError, match="duration_frames must be >= 0"):
            TransitionChoice(kind="dissolve", duration_frames=-1)

    @pytest.mark.parametrize(
        "kind",
        [
            "none", "auto", "dissolve", "fade",
            "slide_left", "slide_right", "slide_top", "slide_bottom",
            "push_left", "push_right", "push_top", "push_bottom",
            "zoom_in", "zoom_out", "flip", "drop",
        ],
    )
    def test_all_documented_kinds_accepted(self, kind):
        TransitionChoice(kind=kind)  # no raise

    def test_to_dict_includes_params_copy(self):
        params = {"easing": "ease_out", "bounce": 0.3}
        t = TransitionChoice(kind="drop", duration_frames=18, params=params)
        d = t.to_dict()
        assert d == {
            "kind": "drop",
            "duration_frames": 18,
            "params": {"easing": "ease_out", "bounce": 0.3},
        }
        d["params"]["easing"] = "linear"  # mutating dict must not affect original
        assert t.params["easing"] == "ease_out"

    def test_round_trip(self):
        t = TransitionChoice(kind="slide_left", duration_frames=12,
                             params={"easing": "ease_in"})
        t2 = TransitionChoice.from_dict(t.to_dict())
        assert t2 == t

    def test_from_dict_tolerates_missing_keys(self):
        t = TransitionChoice.from_dict({})
        assert t.kind == "dissolve"
        assert t.duration_frames == DEFAULT_TRANSITION_DURATION_FRAMES
        assert t.params == {}

    def test_from_dict_tolerates_null_params(self):
        t = TransitionChoice.from_dict({"kind": "fade", "params": None})
        assert t.params == {}


# --------------------------------------------------------------------------- #
# TitleSpec
# --------------------------------------------------------------------------- #

class TestTitleSpec:
    def test_defaults(self):
        t = TitleSpec(text="Hello")
        assert t.text == "Hello"
        assert t.position == "center"
        assert t.style == "Text+"
        assert t.show_for_seconds is None

    def test_rejects_unknown_position(self):
        with pytest.raises(ValueError, match="position must be one of"):
            TitleSpec(text="x", position="diagonal")

    def test_rejects_non_positive_duration(self):
        with pytest.raises(ValueError, match="show_for_seconds must be positive"):
            TitleSpec(text="x", show_for_seconds=0)
        with pytest.raises(ValueError, match="show_for_seconds must be positive"):
            TitleSpec(text="x", show_for_seconds=-2)

    def test_round_trip(self):
        t = TitleSpec(text="Beach Day", position="bottom",
                      style="MyTitleMacro", show_for_seconds=3.5)
        assert TitleSpec.from_dict(t.to_dict()) == t

    def test_from_dict_defaults(self):
        t = TitleSpec.from_dict({"text": "X"})
        assert t.position == "center"
        assert t.style == "Text+"
        assert t.show_for_seconds is None


# --------------------------------------------------------------------------- #
# AudioSettings
# --------------------------------------------------------------------------- #

class TestAudioSettings:
    def test_defaults(self):
        a = AudioSettings(soundtrack_path="C:/songs/x.mp3")
        assert a.soundtrack_path == "C:/songs/x.mp3"
        assert a.beat_sync_enabled is False
        assert a.snap_to == "beat"
        assert a.tolerance_seconds == 0.25
        assert a.analysis_cache_path is None

    def test_rejects_unknown_snap_target(self):
        with pytest.raises(ValueError, match="snap_to must be one of"):
            AudioSettings(soundtrack_path="x.mp3", snap_to="badanimal")

    def test_rejects_negative_tolerance(self):
        with pytest.raises(ValueError, match="tolerance_seconds must be >= 0"):
            AudioSettings(soundtrack_path="x.mp3", tolerance_seconds=-0.1)

    def test_round_trip(self):
        a = AudioSettings(
            soundtrack_path="C:/songs/x.mp3",
            beat_sync_enabled=True,
            snap_to="downbeat",
            tolerance_seconds=0.5,
            analysis_cache_path="C:/cache/x.json",
        )
        assert AudioSettings.from_dict(a.to_dict()) == a


# --------------------------------------------------------------------------- #
# MediaItem
# --------------------------------------------------------------------------- #

class TestMediaItem:
    def test_minimal_construction(self):
        m = MediaItem(path="a.jpg")
        assert m.path == "a.jpg"
        assert m.duration_seconds is None
        assert m.title is None
        assert m.title_text is None
        assert m.outgoing_transition is None
        assert m.locked_duration is False

    def test_duration_override(self):
        m = MediaItem(path="a.jpg", duration_seconds=5.5)
        assert m.duration_seconds == 5.5

    def test_non_positive_duration_collapses_to_none(self):
        # Preserves existing behaviour from the MVP slice — non-positive
        # overrides fall through to the project default.
        assert MediaItem(path="a", duration_seconds=0).duration_seconds is None
        assert MediaItem(path="a", duration_seconds=-1).duration_seconds is None

    def test_title_text_back_compat_kwarg(self):
        m = MediaItem(path="a.jpg", title_text="Hello")
        assert isinstance(m.title, TitleSpec)
        assert m.title.text == "Hello"
        assert m.title_text == "Hello"

    def test_title_and_title_text_mutually_exclusive(self):
        with pytest.raises(TypeError, match="either title"):
            MediaItem(path="a", title=TitleSpec(text="X"), title_text="Y")

    def test_full_construction(self):
        title = TitleSpec(text="Sunset", position="bottom")
        trans = TransitionChoice(kind="slide_left", duration_frames=18)
        m = MediaItem(
            path="a.jpg",
            duration_seconds=3.0,
            title=title,
            outgoing_transition=trans,
            locked_duration=True,
        )
        assert m.title is title
        assert m.outgoing_transition is trans
        assert m.locked_duration is True

    def test_round_trip_minimal(self):
        m = MediaItem(path="a.jpg")
        assert MediaItem.from_dict(m.to_dict()) == m

    def test_round_trip_full(self):
        m = MediaItem(
            path="a.jpg",
            duration_seconds=2.5,
            title=TitleSpec(text="X", position="top", show_for_seconds=1.0),
            outgoing_transition=TransitionChoice(kind="fade", duration_frames=12),
            locked_duration=True,
        )
        assert MediaItem.from_dict(m.to_dict()) == m

    def test_from_dict_accepts_legacy_title_text_key(self):
        m = MediaItem.from_dict({"path": "a.jpg", "title_text": "Old"})
        assert m.title is not None
        assert m.title.text == "Old"


# --------------------------------------------------------------------------- #
# SlideshowProject
# --------------------------------------------------------------------------- #

class TestSlideshowProjectDefaults:
    def test_defaults(self):
        p = SlideshowProject()
        assert p.name == "Slideshow"
        assert p.items == []
        assert p.default_item_duration_seconds == 4.0
        assert p.default_transition.kind == "dissolve"
        assert p.audio is None
        assert p.target_total_duration_seconds is None
        assert p.soundtrack_path is None

    def test_default_transition_is_per_instance(self):
        # Guard against the mutable-default-arg footgun.
        p1 = SlideshowProject()
        p2 = SlideshowProject()
        p1.default_transition.duration_frames = 99
        assert p2.default_transition.duration_frames == DEFAULT_TRANSITION_DURATION_FRAMES


class TestSlideshowProjectFromPaths:
    def test_basic(self):
        p = SlideshowProject.from_paths(["a.jpg", "b.jpg", "c.jpg"])
        assert [it.path for it in p.items] == ["a.jpg", "b.jpg", "c.jpg"]
        assert p.name == "Slideshow"
        assert p.default_item_duration_seconds == 4.0
        assert p.default_transition.kind == "dissolve"

    def test_with_overrides(self):
        p = SlideshowProject.from_paths(
            ["a.jpg"], name="My Show", default_item_duration_seconds=3.0,
            default_transition=TransitionChoice(kind="fade", duration_frames=12),
        )
        assert p.name == "My Show"
        assert p.default_item_duration_seconds == 3.0
        assert p.default_transition.kind == "fade"
        assert p.default_transition.duration_frames == 12


class TestSlideshowProjectEffectiveDuration:
    def test_item_override_wins(self):
        p = SlideshowProject(default_item_duration_seconds=4.0)
        assert p.effective_duration(MediaItem(path="a", duration_seconds=2.5)) == 2.5

    def test_falls_back_to_default_for_none(self):
        p = SlideshowProject(default_item_duration_seconds=4.0)
        assert p.effective_duration(MediaItem(path="a")) == 4.0


class TestTransitionAfter:
    def test_uses_item_override_when_set(self):
        custom = TransitionChoice(kind="slide_left", duration_frames=10)
        p = SlideshowProject(items=[
            MediaItem(path="a", outgoing_transition=custom),
            MediaItem(path="b"),
        ])
        assert p.transition_after(0) is custom

    def test_falls_back_to_default_transition(self):
        p = SlideshowProject(items=[MediaItem(path="a"), MediaItem(path="b")])
        assert p.transition_after(0).kind == "dissolve"

    def test_last_item_returns_hard_cut(self):
        p = SlideshowProject(items=[MediaItem(path="a"), MediaItem(path="b")])
        t = p.transition_after(1)
        assert t.is_cut()
        assert t.kind == "none"

    def test_out_of_range_raises(self):
        p = SlideshowProject(items=[MediaItem(path="a")])
        with pytest.raises(IndexError):
            p.transition_after(5)
        with pytest.raises(IndexError):
            p.transition_after(-1)


class TestTotalDuration:
    def test_sums_effective_durations(self):
        p = SlideshowProject(
            default_item_duration_seconds=3.0,
            items=[
                MediaItem(path="a"),                       # 3.0 (default)
                MediaItem(path="b", duration_seconds=5),   # 5.0
                MediaItem(path="c"),                       # 3.0
            ],
        )
        assert p.total_default_duration_seconds() == pytest.approx(11.0)


class TestSoundtrackBackCompat:
    def test_setter_creates_audio(self):
        p = SlideshowProject()
        p.soundtrack_path = "C:/song.mp3"
        assert p.audio is not None
        assert p.audio.soundtrack_path == "C:/song.mp3"

    def test_setter_replaces_existing(self):
        p = SlideshowProject(audio=AudioSettings(soundtrack_path="x.mp3",
                                                 beat_sync_enabled=True))
        p.soundtrack_path = "y.mp3"
        assert p.audio.soundtrack_path == "y.mp3"
        # Preserves other audio settings.
        assert p.audio.beat_sync_enabled is True

    def test_setter_none_clears_audio(self):
        p = SlideshowProject(audio=AudioSettings(soundtrack_path="x.mp3"))
        p.soundtrack_path = None
        assert p.audio is None
        assert p.soundtrack_path is None


class TestValidate:
    def test_valid_project(self):
        p = SlideshowProject.from_paths(["a.jpg", "b.jpg"])
        assert p.validate() == []

    def test_zero_default_duration(self):
        p = SlideshowProject(default_item_duration_seconds=0)
        problems = p.validate()
        assert any("default_item_duration_seconds" in m for m in problems)

    def test_negative_target_duration(self):
        p = SlideshowProject(target_total_duration_seconds=-1.0)
        problems = p.validate()
        assert any("target_total_duration_seconds" in m for m in problems)

    def test_empty_item_path(self):
        p = SlideshowProject(items=[MediaItem(path=""), MediaItem(path="b")])
        problems = p.validate()
        assert any("items[0]" in m for m in problems)
        assert not any("items[1]" in m for m in problems)


# --------------------------------------------------------------------------- #
# JSON round-trip
# --------------------------------------------------------------------------- #

class TestJsonRoundTrip:
    def _make_full_project(self) -> SlideshowProject:
        return SlideshowProject(
            name="Hawaii 2024",
            default_item_duration_seconds=5.0,
            default_transition=TransitionChoice(kind="fade", duration_frames=18),
            audio=AudioSettings(
                soundtrack_path="C:/music/aloha.mp3",
                beat_sync_enabled=True,
                snap_to="downbeat",
                tolerance_seconds=0.4,
            ),
            target_total_duration_seconds=120.0,
            items=[
                MediaItem(
                    path="C:/pics/1.jpg",
                    title=TitleSpec(text="Arrival", position="bottom"),
                ),
                MediaItem(
                    path="C:/pics/2.jpg",
                    duration_seconds=3.0,
                    outgoing_transition=TransitionChoice(
                        kind="slide_left", duration_frames=12,
                        params={"easing": "ease_out"},
                    ),
                ),
                MediaItem(
                    path="C:/pics/3.jpg",
                    locked_duration=True,
                ),
            ],
        )

    def test_to_from_dict_round_trip(self):
        p = self._make_full_project()
        p2 = SlideshowProject.from_dict(p.to_dict())
        assert p2 == p

    def test_to_from_json_round_trip(self):
        p = self._make_full_project()
        p2 = SlideshowProject.from_json(p.to_json())
        assert p2 == p

    def test_to_dict_contains_schema_version(self):
        p = SlideshowProject()
        assert p.to_dict()["schema_version"] == PROJECT_SCHEMA_VERSION

    def test_to_json_is_valid_unicode_text(self):
        p = SlideshowProject(items=[
            MediaItem(path="a.jpg", title=TitleSpec(text="Café résumé ☕")),
        ])
        text = p.to_json()
        # No escaped Unicode; ensure_ascii=False is intentional.
        assert "Café" in text
        # And it round-trips through stdlib json cleanly.
        json.loads(text)

    def test_save_and_load_from_file(self):
        p = self._make_full_project()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "show.sscproj")
            p.save_to_file(path)
            loaded = SlideshowProject.load_from_file(path)
        assert loaded == p

    def test_from_dict_rejects_newer_schema(self):
        with pytest.raises(ValueError, match="newer than supported"):
            SlideshowProject.from_dict({
                "schema_version": PROJECT_SCHEMA_VERSION + 1,
                "name": "X",
            })

    def test_from_dict_accepts_legacy_soundtrack_path_field(self):
        # Older saved projects had a top-level soundtrack_path string, no
        # AudioSettings block. Make sure those still load cleanly.
        loaded = SlideshowProject.from_dict({
            "name": "Legacy",
            "soundtrack_path": "C:/old.mp3",
            "items": [],
        })
        assert loaded.audio is not None
        assert loaded.audio.soundtrack_path == "C:/old.mp3"

    def test_from_dict_tolerates_missing_optional_fields(self):
        # A barely-populated dict should still produce a valid project
        # using all the documented defaults.
        loaded = SlideshowProject.from_dict({})
        assert loaded.name == "Slideshow"
        assert loaded.items == []
        assert loaded.default_item_duration_seconds == 4.0
        assert loaded.default_transition.kind == "dissolve"
        assert loaded.audio is None
        assert loaded.target_total_duration_seconds is None
