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
    reverse_keyframes,
    wants_outgoing_on_top,
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
from slideshow.fusion_comps import DEFAULT_PAGE_FOCAL_LENGTH, PageTurnAnimation
from slideshow.transitions.page_turn import PAGE_START_ANGLE, page_angle_keys


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

    @pytest.mark.parametrize(
        "kind", ["additive_dissolve", "non_additive_dissolve"]
    )
    def test_non_normal_composites_fade_the_outgoing_clip_out(self, kind):
        """Add and Lighten are only the identity over black.

        Left composited against a fully opaque outgoing clip, the incoming
        image stays blown out for the whole overlap and then snaps to the
        real picture at the cut. Fading the outgoing clip out underneath —
        it is on the bottom track, where transparent renders black — makes
        the transition actually resolve.
        """
        plan = plan_transition(TransitionChoice(kind=kind, duration_frames=24))
        assert plan.outgoing.blend == [(0, 1.0), (24, 0.0)]

    @pytest.mark.parametrize("kind", ["dissolve", "cross_fade"])
    def test_normal_composites_leave_the_outgoing_clip_alone(self, kind):
        """A plain dissolve crossfades *over* an opaque clip; fading that
        clip out too would dip through black."""
        plan = plan_transition(TransitionChoice(kind=kind, duration_frames=24))
        assert plan.outgoing.is_empty()


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
        # The whole dip rides on the upper (incoming) clip now.
        assert plan.outgoing.is_empty()

    def test_dip_to_color_respects_param(self):
        plan = plan_transition(
            TransitionChoice(
                kind="dip_to_color",
                duration_frames=24,
                params={"color": (1.0, 0.0, 0.0)},
            )
        )
        assert plan.incoming.background_color == (1.0, 0.0, 0.0)
        assert plan.outgoing.is_empty()

    def test_dip_to_color_blend_envelope(self):
        # Overall opacity ramps up over the first half (fading the colour
        # in over the clip below); the image only emerges from the colour
        # over the second half.
        plan = plan_transition(
            TransitionChoice(kind="dip_to_color", duration_frames=24)
        )
        assert plan.incoming.blend == [(0, 0.0), (12, 1.0)]
        assert plan.incoming.color_blend == [(0, 0.0), (12, 0.0), (24, 1.0)]

    def test_dip_to_color_mirrored_reveals_the_clip_below(self):
        # When the incoming clip is on the lower track the outgoing clip has
        # to do the work: opaque image, then opaque colour, then gone.
        plan = plan_transition(
            TransitionChoice(kind="dip_to_color", duration_frames=24),
            incoming_on_top=False,
        )
        assert plan.incoming.is_empty()
        assert plan.outgoing.background_color == (0.0, 0.0, 0.0)
        assert plan.outgoing.blend == [(12, 1.0), (24, 0.0)]
        assert plan.outgoing.color_blend == [(0, 1.0), (12, 0.0), (24, 0.0)]

    def test_dip_to_color_too_short_to_dip_degrades_to_a_crossfade(self):
        plan = plan_transition(
            TransitionChoice(kind="dip_to_color", duration_frames=1)
        )
        assert plan.incoming.background_color is None
        assert plan.incoming.blend == [(0, 0.0), (1, 1.0)]


class TestFades:
    def test_fade_dips_through_black(self):
        plan = plan_transition(TransitionChoice(kind="fade", duration_frames=24))
        assert plan.kind == "fade"
        assert plan.incoming.background_color == (0.0, 0.0, 0.0)
        assert plan.outgoing.is_empty()

    def test_fade_through_gray_dips_through_grey(self):
        plan = plan_transition(
            TransitionChoice(kind="fade_through_gray", duration_frames=24)
        )
        assert plan.kind == "fade_through_gray"
        assert plan.incoming.background_color == (0.5, 0.5, 0.5)
        assert plan.outgoing.is_empty()

    def test_blur_through_black_has_blur_envelope(self):
        plan = plan_transition(
            TransitionChoice(kind="blur_through_black", duration_frames=24)
        )
        assert plan.kind == "blur_through_black"
        assert plan.incoming.background_color == (0.0, 0.0, 0.0)
        # The outgoing clip carries no dip — only its own blur, which is
        # what sells the first half while it is still visible underneath.
        assert plan.outgoing.background_color is None
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
        peak = DEFAULT_PIXELATE_PEAK_SIZE
        # Outgoing coarsens up to the peak and then holds there to the end
        # of its window; incoming holds and then resolves.
        assert plan.outgoing.pixelate_size == [(0, 1.0), (8, peak), (24, peak)]
        assert plan.incoming.pixelate_size == [(0, peak), (16, peak), (24, 1.0)]

    def test_pixelate_holds_the_peak_long_enough_to_see(self):
        """The whole point: ramping straight through flashes past."""
        plan = plan_transition(
            TransitionChoice(kind="pixelate", duration_frames=24)
        )
        peak = DEFAULT_PIXELATE_PEAK_SIZE
        out_hold = [f for f, v in plan.outgoing.pixelate_size if v == peak]
        in_hold = [f for f, v in plan.incoming.pixelate_size if v == peak]
        # Frames 8..24 on the outgoing, 0..16 on the incoming — and those
        # are different clips, so the visible hold is 8 frames either side
        # of the midpoint at frame 12.
        assert out_hold[0] == 8
        assert in_hold[-1] == 16
        assert in_hold[-1] - out_hold[0] == 8

    def test_pixelate_swaps_inside_the_hold(self):
        """A long crossfade of two pixelated images is just mud."""
        plan = plan_transition(
            TransitionChoice(kind="pixelate", duration_frames=24)
        )
        assert plan.incoming.blend == [(0, 0.0), (8, 0.0), (16, 1.0)]

    def test_pixelate_hold_param(self):
        """``hold=0`` collapses to the old ramp-straight-through shape."""
        plan = plan_transition(
            TransitionChoice(
                kind="pixelate", duration_frames=24, params={"hold": 0.0}
            )
        )
        assert plan.incoming.blend == [(0, 0.0), (12, 1.0)]

    def test_pixelate_hold_cannot_swallow_the_ramps(self):
        plan = plan_transition(
            TransitionChoice(
                kind="pixelate", duration_frames=24, params={"hold": 5.0}
            )
        )
        frames = [f for f, _ in plan.outgoing.pixelate_size]
        assert frames == sorted(set(frames))
        assert frames[1] >= 1

    def test_pixelate_short_duration_stays_valid(self):
        for duration in (1, 2, 3, 4, 5):
            plan = plan_transition(
                TransitionChoice(kind="pixelate", duration_frames=duration)
            )
            for keys in (plan.outgoing.pixelate_size, plan.incoming.pixelate_size,
                         plan.incoming.blend):
                frames = [f for f, _ in keys]
                assert frames == sorted(set(frames)), (duration, keys)

    def test_pixelate_peak_param(self):
        plan = plan_transition(
            TransitionChoice(
                kind="pixelate",
                duration_frames=24,
                params={"peak_size": 100.0},
            )
        )
        assert plan.outgoing.pixelate_size[1] == (8, 100.0)
        assert plan.incoming.pixelate_size[0] == (0, 100.0)


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

    def test_flip_blend_handoff_is_a_short_crossfade(self):
        """A single-frame swap reads as a glitch — the two images are at
        different angles either side of it, so the cut is plainly visible."""
        plan = plan_transition(TransitionChoice(kind="flip", duration_frames=24))
        assert plan.outgoing.blend == [(10, 1.0), (14, 0.0)]
        assert plan.incoming.blend == [(10, 0.0), (14, 1.0)]
        # Centred on the midpoint, and short enough to still be a flip.
        assert (10 + 14) / 2 == 12
        assert 14 - 10 < 24 // 2

    def test_flip_swap_window_stays_inside_a_short_overlap(self):
        for duration in (1, 2, 3, 4, 6, 8):
            plan = plan_transition(
                TransitionChoice(kind="flip", duration_frames=duration)
            )
            for keys in (plan.outgoing.blend, plan.incoming.blend):
                frames = [f for f, _ in keys]
                assert frames == sorted(set(frames)), (duration, keys)
                assert frames[0] >= 0 and frames[-1] <= duration, (duration, keys)

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

    def test_drop_accelerates_then_bounces_up_from_centre(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        center = plan.incoming.transform.center
        frames = [f for f, _ in center]
        ys = [y for _, (_x, y) in center]

        assert frames == sorted(set(frames))
        assert all(x == 0.5 for _f, (x, _y) in center)

        # Gravity: most of the fall happens in the back half of the descent.
        impact = ys.index(min(ys[: len(ys) // 2 + 1]))
        travelled_by_half = 1.5 - ys[impact // 2]
        assert travelled_by_half < (1.5 - 0.5) / 2, "descent looks linear"

        # It lands *at* centre and bounces back up, rather than overshooting
        # below centre — overshooting below is a spring, not a falling object.
        assert min(ys) >= 0.5
        assert max(ys[impact:]) > 0.5

    def test_drop_bounces_decay(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        ys = [y for _f, (_x, y) in plan.incoming.transform.center]
        peaks = [y for y in ys[ys.index(0.5):] if y > 0.5]
        assert len(peaks) >= 2, "a single bounce reads as a glitch"
        assert peaks[1] - 0.5 < (peaks[0] - 0.5) / 2

    def test_drop_does_not_animate_outgoing(self):
        plan = plan_transition(TransitionChoice(kind="drop", duration_frames=24))
        assert plan.outgoing.is_empty()

    def test_drop_short_duration_still_has_monotonic_keyframes(self):
        # Forces the de-duplication path in drop.py to kick in.
        for duration in (1, 2, 3, 4, 5, 8):
            plan = plan_transition(
                TransitionChoice(kind="drop", duration_frames=duration)
            )
            center = plan.incoming.transform.center
            frames = [k[0] for k in center]
            assert frames == sorted(set(frames)), duration
            assert frames[-1] == duration, duration
            # However little room there is, it must still end at rest.
            assert center[-1] == (duration, (0.5, 0.5)), duration


# --------------------------------------------------------------------------- #
# Mirroring — adapting a plan when the incoming clip is on the lower track
# --------------------------------------------------------------------------- #

class TestMirroring:
    def test_reverse_keyframes_flips_time_not_values(self):
        assert reverse_keyframes([(0, 0.0), (10, 1.0)], 10) == [(0, 1.0), (10, 0.0)]
        assert reverse_keyframes([(0, "a"), (4, "b"), (10, "c")], 10) == [
            (0, "c"),
            (6, "b"),
            (10, "a"),
        ]

    def test_reverse_keyframes_of_nothing_is_none(self):
        assert reverse_keyframes(None, 10) is None
        assert reverse_keyframes([], 10) is None

    @pytest.mark.parametrize("kind", sorted(registered_kinds()))
    def test_every_kind_can_be_mirrored(self, kind):
        choice = TransitionChoice(kind=kind, duration_frames=24)
        mirrored = plan_transition(choice, incoming_on_top=False)
        assert mirrored.kind == kind
        upright = plan_transition(choice, incoming_on_top=True)
        assert mirrored.duration_frames == upright.duration_frames

    @pytest.mark.parametrize("kind", sorted(registered_kinds()))
    def test_mirrored_plans_animate_the_upper_clip(self, kind):
        """The outgoing clip is the visible one, so it must do the work.

        Anything that only animates the incoming (lower) clip would be
        hidden under the opaque outgoing clip and render as a hard cut.
        """
        choice = TransitionChoice(kind=kind, duration_frames=24)
        upright = plan_transition(choice, incoming_on_top=True)
        if upright.is_cut:
            pytest.skip("{0} is a cut".format(kind))
        mirrored = plan_transition(choice, incoming_on_top=False)
        assert not mirrored.outgoing.is_empty()

    def test_dissolve_mirrors_to_a_fade_out(self):
        mirrored = plan_transition(
            TransitionChoice(kind="dissolve", duration_frames=24),
            incoming_on_top=False,
        )
        assert mirrored.incoming.is_empty()
        assert mirrored.outgoing.blend == [(0, 1.0), (24, 0.0)]

    def test_additive_dissolve_moves_its_composite_mode_to_the_top_clip(self):
        mirrored = plan_transition(
            TransitionChoice(kind="additive_dissolve", duration_frames=24),
            incoming_on_top=False,
        )
        assert mirrored.outgoing.composite_mode == "add"

    def test_slide_left_mirrors_by_exiting_left(self):
        # Not the time-reverse, which would travel rightward and make the
        # named direction meaningless on every other transition.
        mirrored = plan_transition(
            TransitionChoice(kind="slide_left", duration_frames=24),
            incoming_on_top=False,
        )
        assert mirrored.incoming.is_empty()
        assert mirrored.outgoing.transform.center == [
            (0, (0.5, 0.5)),
            (24, (-0.5, 0.5)),
        ]

    @pytest.mark.parametrize(
        "kind,exit_point",
        [
            ("slide_left", (-0.5, 0.5)),
            ("slide_right", (1.5, 0.5)),
            ("slide_top", (0.5, 1.5)),
            ("slide_bottom", (0.5, -0.5)),
        ],
    )
    def test_every_slide_keeps_its_direction_when_mirrored(self, kind, exit_point):
        mirrored = plan_transition(
            TransitionChoice(kind=kind, duration_frames=24), incoming_on_top=False
        )
        assert mirrored.outgoing.transform.center[-1] == (24, exit_point)

    @pytest.mark.parametrize(
        "kind", ["push_left", "push_right", "push_top", "push_bottom"]
    )
    def test_pushes_are_unaffected_by_stacking_order(self, kind):
        choice = TransitionChoice(kind=kind, duration_frames=24)
        mirrored = plan_transition(choice, incoming_on_top=False)
        upright = plan_transition(choice, incoming_on_top=True)
        assert mirrored.incoming.transform.center == upright.incoming.transform.center
        assert mirrored.outgoing.transform.center == upright.outgoing.transform.center

    def test_drop_still_falls_downwards_when_mirrored(self):
        mirrored = plan_transition(
            TransitionChoice(kind="drop", duration_frames=24), incoming_on_top=False
        )
        assert mirrored.incoming.is_empty()
        center = mirrored.outgoing.transform.center
        assert center[0] == (0, (0.5, 0.5))
        assert center[-1] == (24, (0.5, -0.5))
        frames = [k[0] for k in center]
        assert frames == sorted(frames)
        assert len(set(frames)) == len(frames)

    def test_a_cut_mirrors_to_itself(self):
        choice = TransitionChoice(kind="none", duration_frames=24)
        assert plan_transition(choice, incoming_on_top=False) == plan_transition(
            choice, incoming_on_top=True
        )


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
        assert plan.outgoing.is_empty()

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



# --------------------------------------------------------------------------- #
# Page turn
# --------------------------------------------------------------------------- #

class TestPageTurn:
    def test_only_the_incoming_page_moves(self):
        plan = plan_transition(
            TransitionChoice(kind="page_turn", duration_frames=36)
        )

        assert plan.incoming.page_turn is not None
        # The photo being covered up does nothing, exactly like the page under
        # the one being turned.
        assert plan.outgoing.is_empty()

    def test_the_page_comes_to_rest_exactly_flat(self):
        # Any residual angle on the last frame shows up as a visible jump,
        # because the next frame is the body segment showing the plain photo.
        for duration in (2, 5, 12, 24, 36, 120):
            plan = plan_transition(
                TransitionChoice(kind="page_turn", duration_frames=duration)
            )
            angle = plan.incoming.page_turn.angle
            assert angle[-1] == (duration, 0.0)
            assert angle[0][0] == 0
            assert angle[0][1] == pytest.approx(PAGE_START_ANGLE)

    def test_keyframes_are_strictly_increasing_at_any_length(self):
        for duration in range(1, 60):
            keys = page_angle_keys(duration, PAGE_START_ANGLE)
            frames = [frame for frame, _ in keys]
            assert frames == sorted(set(frames)), duration
            assert frames[-1] == duration

    def test_the_arc_is_front_loaded(self):
        keys = page_angle_keys(100, 100.0)
        midpoint = [value for frame, value in keys if frame == 55][0]
        # Past halfway in time the page should be most of the way down, not
        # halfway -- a linear sweep reads mechanical.
        assert midpoint < 50.0

    def test_hinge_defaults_to_right_and_can_be_overridden(self):
        default = plan_transition(
            TransitionChoice(kind="page_turn", duration_frames=24)
        )
        left = plan_transition(
            TransitionChoice(
                kind="page_turn", duration_frames=24, params={"hinge": "left"}
            )
        )

        assert default.incoming.page_turn.hinge == "right"
        assert left.incoming.page_turn.hinge == "left"

    def test_nonsense_params_fall_back_to_the_defaults(self):
        plan = plan_transition(
            TransitionChoice(
                kind="page_turn",
                duration_frames=24,
                params={"hinge": "sideways", "focal_length": "wide", "angle": None},
            )
        )

        page = plan.incoming.page_turn
        assert page.hinge == "right"
        assert page.focal_length == DEFAULT_PAGE_FOCAL_LENGTH
        assert page.angle[0][1] == pytest.approx(PAGE_START_ANGLE)

    def test_a_negative_focal_length_is_ignored(self):
        plan = plan_transition(
            TransitionChoice(
                kind="page_turn", duration_frames=24, params={"focal_length": -5}
            )
        )

        assert plan.incoming.page_turn.focal_length == DEFAULT_PAGE_FOCAL_LENGTH

    def test_zero_duration_is_a_cut(self):
        plan = plan_transition(
            TransitionChoice(kind="page_turn", duration_frames=0)
        )

        assert plan.is_cut
        assert plan.incoming.page_turn is None

    def test_mirroring_makes_the_page_leave_instead_of_arrive(self):
        plan = plan_transition(
            TransitionChoice(kind="page_turn", duration_frames=24),
            incoming_on_top=False,
        )

        # Mirrored, the clip on top is the outgoing one and its page swings
        # back out the way it came.
        page = plan.outgoing.page_turn
        assert page is not None
        assert page.angle[0] == (0, 0.0)
        assert page.angle[-1][0] == 24
        assert page.angle[-1][1] == pytest.approx(PAGE_START_ANGLE)
        # The hinge is a property of the page, not of the direction of travel.
        assert page.hinge == "right"

    def test_page_turn_cannot_be_stacked_on_the_2d_chain(self):
        # They are different pipelines -- silently dropping one would be worse.
        with pytest.raises(ValueError):
            ClipPlan(
                page_turn=PageTurnAnimation(angle=[(0, 90.0), (10, 0.0)]),
                blur_size=[(0, 10.0), (10, 0.0)],
            )

    def test_an_empty_page_turn_does_not_block_the_2d_chain(self):
        plan = ClipPlan(
            page_turn=PageTurnAnimation(), blur_size=[(0, 10.0), (10, 0.0)]
        )

        assert plan.page_turn.is_empty()
        assert not plan.is_empty()

    def test_hinge_and_focal_length_are_validated(self):
        with pytest.raises(ValueError):
            PageTurnAnimation(hinge="middle")
        with pytest.raises(ValueError):
            PageTurnAnimation(focal_length=0)


# --------------------------------------------------------------------------- #
# Which side goes on the upper track
# --------------------------------------------------------------------------- #


class TestWantsOutgoingOnTop:
    def test_ordinary_transitions_want_the_incoming_on_top(self):
        for kind in ("dissolve", "fade", "slide_left", "flip", "page_turn"):
            choice = TransitionChoice(kind=kind, duration_frames=12)
            assert wants_outgoing_on_top(choice) is False

    def test_page_turn_away_wants_the_outgoing_on_top(self):
        choice = TransitionChoice(kind="page_turn_away", duration_frames=12)
        assert wants_outgoing_on_top(choice) is True

    def test_cuts_and_missing_choices_answer_no(self):
        assert wants_outgoing_on_top(None) is False
        assert wants_outgoing_on_top(TransitionChoice(kind="none")) is False
        assert (
            wants_outgoing_on_top(
                TransitionChoice(kind="dissolve", duration_frames=0)
            )
            is False
        )

    def test_unresolved_auto_answers_no(self):
        # "auto" has no implementation registered, so it must degrade to the
        # ordinary layout rather than raise. The builder resolves it first.
        assert wants_outgoing_on_top(TransitionChoice(kind="auto")) is False


class TestPageTurnAway:
    def test_it_asks_for_the_outgoing_slide_on_top(self):
        assert get_transition("page_turn_away").PREFERS_OUTGOING_ON_TOP is True
        assert get_transition("page_turn").PREFERS_OUTGOING_ON_TOP is False

    def test_it_hinges_on_the_opposite_edge_to_page_turn(self):
        away = plan_transition(
            TransitionChoice(kind="page_turn_away", duration_frames=36)
        )
        assert away.incoming.page_turn.hinge == "left"
        toward = plan_transition(
            TransitionChoice(kind="page_turn", duration_frames=36)
        )
        assert toward.incoming.page_turn.hinge == "right"

    def test_mirroring_moves_the_rotation_onto_the_outgoing_clip(self):
        plan = plan_transition(
            TransitionChoice(kind="page_turn_away", duration_frames=36),
            incoming_on_top=False,
        )
        assert plan.incoming.page_turn is None
        assert plan.outgoing.page_turn is not None

    def test_the_mirrored_page_starts_flat_and_swings_out(self):
        plan = plan_transition(
            TransitionChoice(kind="page_turn_away", duration_frames=36),
            incoming_on_top=False,
        )
        angles = plan.outgoing.page_turn.angle
        assert angles[0][0] == 0
        assert angles[0][1] == pytest.approx(0.0)
        assert angles[-1][0] == 36
        assert angles[-1][1] == pytest.approx(PAGE_START_ANGLE)

    def test_hinge_is_still_overridable(self):
        plan = plan_transition(
            TransitionChoice(
                kind="page_turn_away",
                duration_frames=24,
                params={"hinge": "right"},
            )
        )
        assert plan.incoming.page_turn.hinge == "right"
