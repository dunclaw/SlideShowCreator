"""Unit tests for the transition planning framework.

Pure-Python tests — no Resolve involved. Each test pokes one
:class:`Transition` impl through :func:`plan_transition` and asserts
the resulting :class:`TransitionPlan` has the right shape:
* expected ``kind`` / ``duration_frames``
* keyframe endpoints land at the right normalised coordinates
* symmetry properties hold (e.g. slide_left and slide_right mirror)
* edge cases — 0-duration collapses to a cut, ``auto`` raises, unknown
  kinds raise.
"""

from __future__ import annotations

import pytest

from slideshow.project_model import TRANSITION_KINDS, TransitionChoice
from slideshow.transitions import (
    COMPOSITE_MODES,
    ClipPlan,
    TransitionPlan,
    get_transition,
    plan_transition,
    registered_kinds,
)
from slideshow.transitions.dissolves import (
    DEFAULT_BLUR_DISSOLVE_PEAK_SIZE,
    _parse_color,
)
from slideshow.transitions.effects import (
    DEFAULT_PIXELATE_PEAK_SIZE,
    SMOOTH_CUT_MAX_FRAMES,
)
from slideshow.transitions.geometry import (
    DEFAULT_ZOOM_IN_START_SIZE,
    DEFAULT_ZOOM_OUT_START_SIZE,
)


# --------------------------------------------------------------------------- #
# Registry coverage
# --------------------------------------------------------------------------- #

class TestRegistryCoverage:
    """Every concrete kind in the project model must have an impl."""

    def test_registered_kinds_cover_model_minus_auto(self):
        registered = set(registered_kinds())
        expected = set(TRANSITION_KINDS) - {"auto"}
        assert registered == expected

    def test_no_unexpected_kinds_registered(self):
        registered = set(registered_kinds())
        assert registered <= set(TRANSITION_KINDS)

    def test_all_kinds_dispatch(self):
        for kind in registered_kinds():
            choice = TransitionChoice(kind=kind, duration_frames=24)
            plan = plan_transition(choice)
            assert plan.kind == kind
            assert isinstance(plan, TransitionPlan)


# --------------------------------------------------------------------------- #
# ClipPlan / TransitionPlan basics
# --------------------------------------------------------------------------- #

class TestClipPlanBasics:
    def test_default_clip_plan_is_empty(self):
        assert ClipPlan().is_empty()

    def test_clip_plan_with_blend_is_not_empty(self):
        assert not ClipPlan(blend=[(0, 0.0), (10, 1.0)]).is_empty()

    def test_clip_plan_with_composite_mode_is_not_empty(self):
        assert not ClipPlan(composite_mode="add").is_empty()

    def test_invalid_composite_mode_raises(self):
        with pytest.raises(ValueError):
            ClipPlan(composite_mode="multiply")

    def test_composite_mode_values(self):
        assert COMPOSITE_MODES == {"normal", "add", "non_add"}

    def test_transition_plan_default_is_cut(self):
        p = TransitionPlan(kind="none", duration_frames=0)
        assert p.is_cut

    def test_transition_plan_with_effects_is_not_cut(self):
        p = TransitionPlan(
            kind="dissolve",
            duration_frames=24,
            incoming=ClipPlan(blend=[(0, 0.0), (24, 1.0)]),
        )
        assert not p.is_cut


# --------------------------------------------------------------------------- #
# Dispatcher errors
# --------------------------------------------------------------------------- #

class TestDispatcherErrors:
    def test_auto_raises_value_error(self):
        with pytest.raises(ValueError, match="auto"):
            plan_transition(TransitionChoice(kind="auto", duration_frames=24))

    def test_auto_raises_via_get_transition(self):
        with pytest.raises(ValueError):
            get_transition("auto")

    def test_unknown_kind_raises_key_error(self):
        # Bypass TransitionChoice validation by patching after construction.
        choice = TransitionChoice(kind="dissolve", duration_frames=12)
        choice.kind = "totally_made_up_kind"
        with pytest.raises(KeyError):
            plan_transition(choice)

    def test_negative_duration_raises(self):
        choice = TransitionChoice(kind="dissolve", duration_frames=12)
        choice.duration_frames = -1
        with pytest.raises(ValueError):
            plan_transition(choice)


# --------------------------------------------------------------------------- #
# Zero-duration uniformly collapses to a cut
# --------------------------------------------------------------------------- #

class TestZeroDurationCollapsesToCut:
    @pytest.mark.parametrize("kind", sorted(set(TRANSITION_KINDS) - {"auto"}))
    def test_zero_duration_produces_cut(self, kind):
        choice = TransitionChoice(kind=kind, duration_frames=0)
        plan = plan_transition(choice)
        assert plan.kind == kind
        assert plan.is_cut, "{0} should collapse to a cut at duration=0".format(kind)


# --------------------------------------------------------------------------- #
# Dissolve family
# --------------------------------------------------------------------------- #

class TestDissolve:
    def test_dissolve_fades_incoming_zero_to_one(self):
        plan = plan_transition(TransitionChoice(kind="dissolve", duration_frames=24))
        assert plan.incoming.blend == [(0, 0.0), (24, 1.0)]
        assert plan.outgoing.is_empty()
        assert plan.incoming.composite_mode == "normal"

    def test_cross_fade_matches_dissolve_shape(self):
        a = plan_transition(TransitionChoice(kind="dissolve", duration_frames=24))
        b = plan_transition(TransitionChoice(kind="cross_fade", duration_frames=24))
        assert a.incoming.blend == b.incoming.blend
        assert a.incoming.composite_mode == b.incoming.composite_mode

    def test_additive_dissolve_uses_add_composite(self):
        plan = plan_transition(
            TransitionChoice(kind="additive_dissolve", duration_frames=24)
        )
        assert plan.incoming.composite_mode == "add"

    def test_non_additive_dissolve_uses_non_add_composite(self):
        plan = plan_transition(
            TransitionChoice(kind="non_additive_dissolve", duration_frames=24)
        )
        assert plan.incoming.composite_mode == "non_add"


class TestBlurDissolve:
    def test_blur_dissolve_has_blur_on_both_clips(self):
        plan = plan_transition(
            TransitionChoice(kind="blur_dissolve", duration_frames=24)
        )
        assert plan.incoming.blur_size is not None
        assert plan.outgoing.blur_size is not None
        # Endpoints reach the default peak.
        peak = DEFAULT_BLUR_DISSOLVE_PEAK_SIZE
        assert plan.incoming.blur_size[0] == (0, peak)
        assert plan.incoming.blur_size[-1] == (24, 0.0)
        assert plan.outgoing.blur_size[0] == (0, 0.0)
        assert plan.outgoing.blur_size[-1] == (24, peak)

    def test_blur_dissolve_peak_size_param_overrides(self):
        plan = plan_transition(
            TransitionChoice(
                kind="blur_dissolve",
                duration_frames=24,
                params={"peak_size": 50.0},
            )
        )
        assert plan.incoming.blur_size[0] == (0, 50.0)
        assert plan.outgoing.blur_size[-1] == (24, 50.0)

    def test_blur_dissolve_still_blends_incoming(self):
        plan = plan_transition(
            TransitionChoice(kind="blur_dissolve", duration_frames=24)
        )
        assert plan.incoming.blend == [(0, 0.0), (24, 1.0)]


# --------------------------------------------------------------------------- #
# Dip to color + fades
# --------------------------------------------------------------------------- #

class TestParseColor:
    def test_none_returns_default_black(self):
        assert _parse_color(None) == (0.0, 0.0, 0.0)

    def test_rgb_tuple(self):
        assert _parse_color((0.25, 0.5, 0.75)) == (0.25, 0.5, 0.75)

    def test_rgb_list_clamps(self):
        assert _parse_color([1.5, -0.5, 0.5]) == (1.0, 0.0, 0.5)

    def test_hex_long(self):
        r, g, b = _parse_color("#ff8000")
        assert r == pytest.approx(1.0)
        assert g == pytest.approx(0x80 / 255.0)
        assert b == pytest.approx(0.0)

    def test_hex_short(self):
        r, g, b = _parse_color("#f80")
        assert r == pytest.approx(1.0)
        assert g == pytest.approx(0x88 / 255.0)
        assert b == pytest.approx(0.0)

    def test_hex_no_hash(self):
        assert _parse_color("000000") == (0.0, 0.0, 0.0)

    def test_unparseable_falls_back_to_default(self):
        assert _parse_color("not a color") == (0.0, 0.0, 0.0)


class TestDipToColor:
    def test_dip_to_color_default_is_black(self):
        plan = plan_transition(
            TransitionChoice(kind="dip_to_color", duration_frames=24)
        )
        assert plan.incoming.background_color == (0.0, 0.0, 0.0)
        assert plan.outgoing.background_color == (0.0, 0.0, 0.0)

    def test_dip_to_color_respects_param(self):
        plan = plan_transition(
            TransitionChoice(
                kind="dip_to_color",
                duration_frames=24,
                params={"color": (1.0, 0.0, 0.0)},
            )
        )
        assert plan.incoming.background_color == (1.0, 0.0, 0.0)
        assert plan.outgoing.background_color == (1.0, 0.0, 0.0)

    def test_dip_to_color_blend_envelope(self):
        # Outgoing visible at start, gone by midpoint.
        # Incoming starts at zero at midpoint, full by end.
        plan = plan_transition(
            TransitionChoice(kind="dip_to_color", duration_frames=24)
        )
        assert plan.outgoing.blend[0] == (0, 1.0)
        assert plan.outgoing.blend[-1] == (12, 0.0)
        assert plan.incoming.blend[0] == (12, 0.0)
        assert plan.incoming.blend[-1] == (24, 1.0)


class TestFades:
    def test_fade_dips_through_black(self):
        plan = plan_transition(TransitionChoice(kind="fade", duration_frames=24))
        assert plan.kind == "fade"
        assert plan.incoming.background_color == (0.0, 0.0, 0.0)
        assert plan.outgoing.background_color == (0.0, 0.0, 0.0)

    def test_fade_through_gray_dips_through_grey(self):
        plan = plan_transition(
            TransitionChoice(kind="fade_through_gray", duration_frames=24)
        )
        assert plan.kind == "fade_through_gray"
        assert plan.incoming.background_color == (0.5, 0.5, 0.5)
        assert plan.outgoing.background_color == (0.5, 0.5, 0.5)

    def test_blur_through_black_has_blur_envelope(self):
        plan = plan_transition(
            TransitionChoice(kind="blur_through_black", duration_frames=24)
        )
        assert plan.kind == "blur_through_black"
        assert plan.incoming.background_color == (0.0, 0.0, 0.0)
        assert plan.outgoing.background_color == (0.0, 0.0, 0.0)
        # Outgoing blurs into black: 0 → peak at midpoint.
        assert plan.outgoing.blur_size[0] == (0, 0.0)
        peak = plan.outgoing.blur_size[-1][1]
        assert peak > 0
        # Incoming starts at peak at midpoint, ends sharp.
        assert plan.incoming.blur_size[0][1] == peak
        assert plan.incoming.blur_size[-1] == (24, 0.0)


# --------------------------------------------------------------------------- #
# Effects family
# --------------------------------------------------------------------------- #

class TestNone:
    def test_none_is_always_cut(self):
        plan = plan_transition(TransitionChoice(kind="none", duration_frames=24))
        assert plan.is_cut
        assert plan.duration_frames == 0
        assert plan.incoming.is_empty()
        assert plan.outgoing.is_empty()


class TestPixelate:
    def test_pixelate_animates_both_clips(self):
        plan = plan_transition(
            TransitionChoice(kind="pixelate", duration_frames=24)
        )
        assert plan.incoming.pixelate_size is not None
        assert plan.outgoing.pixelate_size is not None
        # Default peak at midpoint.
        peak = DEFAULT_PIXELATE_PEAK_SIZE
        assert plan.outgoing.pixelate_size[0] == (0, 1.0)
        assert plan.outgoing.pixelate_size[-1] == (12, peak)
        assert plan.incoming.pixelate_size[0] == (12, peak)
        assert plan.incoming.pixelate_size[-1] == (24, 1.0)

    def test_pixelate_blends_incoming(self):
        plan = plan_transition(
            TransitionChoice(kind="pixelate", duration_frames=24)
        )
        assert plan.incoming.blend == [(0, 0.0), (24, 1.0)]

    def test_pixelate_peak_param(self):
        plan = plan_transition(
            TransitionChoice(
                kind="pixelate",
                duration_frames=24,
                params={"peak_size": 100.0},
            )
        )
        assert plan.outgoing.pixelate_size[-1] == (12, 100.0)
        assert plan.incoming.pixelate_size[0] == (12, 100.0)


class TestSmoothCut:
    def test_smooth_cut_clamps_to_max(self):
        plan = plan_transition(
            TransitionChoice(kind="smooth_cut", duration_frames=60)
        )
        assert plan.duration_frames == SMOOTH_CUT_MAX_FRAMES
        assert plan.incoming.blend == [(0, 0.0), (SMOOTH_CUT_MAX_FRAMES, 1.0)]

    def test_smooth_cut_short_duration_kept(self):
        plan = plan_transition(
            TransitionChoice(kind="smooth_cut", duration_frames=2)
        )
        assert plan.duration_frames == 2
        assert plan.incoming.blend == [(0, 0.0), (2, 1.0)]


# --------------------------------------------------------------------------- #
# Slide / push geometry
# --------------------------------------------------------------------------- #

# (kind, expected start point, expected end point) for the incoming clip.
SLIDE_CASES = [
    ("slide_left", (1.5, 0.5), (0.5, 0.5)),
    ("slide_right", (-0.5, 0.5), (0.5, 0.5)),
    ("slide_top", (0.5, -0.5), (0.5, 0.5)),
    ("slide_bottom", (0.5, 1.5), (0.5, 0.5)),
]


class TestSlides:
    @pytest.mark.parametrize("kind,start,end", SLIDE_CASES)
    def test_slide_endpoints(self, kind, start, end):
        plan = plan_transition(TransitionChoice(kind=kind, duration_frames=24))
        center = plan.incoming.transform.center
        assert center[0] == (0, start)
        assert center[-1] == (24, end)

    @pytest.mark.parametrize("kind,_start,_end", SLIDE_CASES)
    def test_slide_does_not_animate_outgoing(self, kind, _start, _end):
        plan = plan_transition(TransitionChoice(kind=kind, duration_frames=24))
        assert plan.outgoing.is_empty()

    def test_slide_left_and_right_are_mirror_images(self):
        left = plan_transition(TransitionChoice(kind="slide_left", duration_frames=24))
        right = plan_transition(TransitionChoice(kind="slide_right", duration_frames=24))
        lx0 = left.incoming.transform.center[0][1][0]
        rx0 = right.incoming.transform.center[0][1][0]
        # Mirror about x=0.5
        assert lx0 + rx0 == pytest.approx(1.0)

    def test_slide_top_and_bottom_are_mirror_images(self):
        top = plan_transition(TransitionChoice(kind="slide_top", duration_frames=24))
        bot = plan_transition(TransitionChoice(kind="slide_bottom", duration_frames=24))
        ty0 = top.incoming.transform.center[0][1][1]
        by0 = bot.incoming.transform.center[0][1][1]
        assert ty0 + by0 == pytest.approx(1.0)


PUSH_CASES = [
    ("push_left", (1.5, 0.5), (0.5, 0.5), (-0.5, 0.5)),
    ("push_right", (-0.5, 0.5), (0.5, 0.5), (1.5, 0.5)),
    ("push_top", (0.5, -0.5), (0.5, 0.5), (0.5, 1.5)),
    ("push_bottom", (0.5, 1.5), (0.5, 0.5), (0.5, -0.5)),
]


class TestPushes:
    @pytest.mark.parametrize("kind,in_start,in_end,out_end", PUSH_CASES)
    def test_push_animates_both_clips(self, kind, in_start, in_end, out_end):
        plan = plan_transition(TransitionChoice(kind=kind, duration_frames=24))
        in_center = plan.incoming.transform.center
        out_center = plan.outgoing.transform.center
        assert in_center[0] == (0, in_start)
        assert in_center[-1] == (24, in_end)
        assert out_center[0] == (0, (0.5, 0.5))
        assert out_center[-1] == (24, out_end)

    @pytest.mark.parametrize("kind,_a,_b,_c", PUSH_CASES)
    def test_push_motion_is_parallel(self, kind, _a, _b, _c):
        """Both clips should travel the same direction by the same distance."""
        plan = plan_transition(TransitionChoice(kind=kind, duration_frames=24))
        in_start = plan.incoming.transform.center[0][1]
        in_end = plan.incoming.transform.center[-1][1]
        out_start = plan.outgoing.transform.center[0][1]
        out_end = plan.outgoing.transform.center[-1][1]
        in_delta = (in_end[0] - in_start[0], in_end[1] - in_start[1])
        out_delta = (out_end[0] - out_start[0], out_end[1] - out_start[1])
        assert in_delta == pytest.approx(out_delta)


# --------------------------------------------------------------------------- #
# Zoom
# --------------------------------------------------------------------------- #

class TestZoom:
    def test_zoom_in_grows_from_zero(self):
        plan = plan_transition(TransitionChoice(kind="zoom_in", duration_frames=24))
        size = plan.incoming.transform.size
        assert size[0] == (0, DEFAULT_ZOOM_IN_START_SIZE)
        assert size[-1] == (24, 1.0)

    def test_zoom_in_blends_incoming(self):
        plan = plan_transition(TransitionChoice(kind="zoom_in", duration_frames=24))
        assert plan.incoming.blend == [(0, 0.0), (24, 1.0)]

    def test_zoom_out_shrinks_to_one(self):
        plan = plan_transition(TransitionChoice(kind="zoom_out", duration_frames=24))
        size = plan.incoming.transform.size
        assert size[0] == (0, DEFAULT_ZOOM_OUT_START_SIZE)
        assert size[-1] == (24, 1.0)

    def test_zoom_in_start_size_param(self):
        plan = plan_transition(
            TransitionChoice(
                kind="zoom_in",
                duration_frames=24,
                params={"start_size": 0.25},
            )
        )
        assert plan.incoming.transform.size[0] == (0, 0.25)

    def test_zoom_out_start_size_param(self):
        plan = plan_transition(
            TransitionChoice(
                kind="zoom_out",
                duration_frames=24,
                params={"start_size": 5.0},
            )
        )
        assert plan.incoming.transform.size[0] == (0, 5.0)


# --------------------------------------------------------------------------- #
# Flip
# --------------------------------------------------------------------------- #

class TestFlip:
    def test_flip_animates_both_clips(self):
        plan = plan_transition(TransitionChoice(kind="flip", duration_frames=24))
        assert plan.outgoing.transform is not None
        assert plan.incoming.transform is not None

    def test_flip_outgoing_rotates_to_90(self):
        plan = plan_transition(TransitionChoice(kind="flip", duration_frames=24))
        angle = plan.outgoing.transform.angle
        assert angle[0] == (0, 0.0)
        assert angle[-1] == (12, 90.0)

    def test_flip_incoming_rotates_from_neg90(self):
        plan = plan_transition(TransitionChoice(kind="flip", duration_frames=24))
        angle = plan.incoming.transform.angle
        assert angle[0] == (12, -90.0)
        assert angle[-1] == (24, 0.0)

    def test_flip_blend_handoff_at_midpoint(self):
        plan = plan_transition(TransitionChoice(kind="flip", duration_frames=24))
        # outgoing snaps off at midpoint
        assert plan.outgoing.blend[-1] == (12, 0.0)
        # incoming snaps on at midpoint
        assert plan.incoming.blend[-1] == (12, 1.0)

    def test_flip_vertical_axis_inverts_rotation(self):
        plan = plan_transition(
            TransitionChoice(
                kind="flip",
                duration_frames=24,
                params={"axis": "vertical"},
            )
        )
        # Sign is inverted vs horizontal default.
        assert plan.outgoing.transform.angle[-1] == (12, -90.0)
        assert plan.incoming.transform.angle[0] == (12, 90.0)


# --------------------------------------------------------------------------- #
# Drop
# --------------------------------------------------------------------------- #

class TestDrop:
    def test_drop_starts_above_frame(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        center = plan.incoming.transform.center
        assert center[0] == (0, (0.5, 1.5))

    def test_drop_settles_at_center(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        center = plan.incoming.transform.center
        assert center[-1] == (24, (0.5, 0.5))

    def test_drop_includes_overshoot_and_bounce(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        center = plan.incoming.transform.center
        # 4 keyframes: start, overshoot, bounce, settle.
        assert len(center) == 4
        # Frame indices are strictly increasing.
        frames = [k[0] for k in center]
        assert frames == sorted(frames)
        assert len(set(frames)) == len(frames)

    def test_drop_does_not_animate_outgoing(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        assert plan.outgoing.is_empty()

    def test_drop_short_duration_still_has_monotonic_keyframes(self):
        # Forces the de-duplication path in drop.py to kick in.
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=4))
        frames = [k[0] for k in plan.incoming.transform.center]
        assert frames == sorted(frames)
        assert len(set(frames)) == len(frames)


# --------------------------------------------------------------------------- #
# Round-trip via TransitionChoice with params
# --------------------------------------------------------------------------- #

class TestRoundTrip:
    def test_params_flow_through_to_plan(self):
        choice = TransitionChoice(
            kind="dip_to_color",
            duration_frames=30,
            params={"color": "#ff0000"},
        )
        plan = plan_transition(choice)
        assert plan.duration_frames == 30
        assert plan.incoming.background_color == (1.0, 0.0, 0.0)
        assert plan.outgoing.background_color == (1.0, 0.0, 0.0)

    def test_plan_does_not_mutate_choice_params(self):
        choice = TransitionChoice(
            kind="blur_dissolve",
            duration_frames=24,
            params={"peak_size": 33.0},
        )
        plan_transition(choice)
        assert choice.params == {"peak_size": 33.0}

    def test_unknown_params_are_ignored(self):
        # Should not raise; unknown keys ignored across all impls.
        for kind in registered_kinds():
            choice = TransitionChoice(
                kind=kind,
                duration_frames=24,
                params={"unrecognised_key": 999},
            )
            plan_transition(choice)
