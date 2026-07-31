"""Dissolve-family transitions.

All dissolves share the same fundamental shape — the incoming clip's
opacity ramps 0 → 1 over the overlap while the outgoing clip stays
visible underneath. They differ in how the two clips blend together:

* :class:`Dissolve` / :class:`CrossFade` — straight alpha composite.
  (``cross_fade`` is the Movie-Maker label for the same effect; we keep
  both kinds so the UI can preserve the user's chosen name.)
* :class:`AdditiveDissolve` — additive blend; bright areas of each
  clip add together, briefly producing a "hot" mid-transition look.
* :class:`NonAdditiveDissolve` — max-blend; takes the brighter pixel
  of the two clips, creating a softer crossover with less bloom.
* :class:`BlurDissolve` — straight dissolve plus animated blur on
  both clips that peaks at mid-transition and drops back to 0.
* :class:`DipToColor` — both clips fade to a solid colour at
  mid-transition rather than to each other. The colour is read from
  ``params["color"]`` and falls back to black; supported as an RGB
  triple ``(r, g, b)`` in ``[0, 1]`` or a ``"#rrggbb"`` / ``"#rgb"``
  hex string.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ClipPlan, RgbColor, Transition, TransitionPlan, register


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

DEFAULT_BLUR_DISSOLVE_PEAK_SIZE = 25.0   # Fusion Blur 'Size' units
DEFAULT_DIP_COLOR: RgbColor = (0.0, 0.0, 0.0)  # dip to black if no color given


def _empty_plan(kind: str) -> TransitionPlan:
    """A no-op plan — used when duration_frames == 0."""
    return TransitionPlan(kind=kind, duration_frames=0)


def _fade_in_keys(duration_frames: int) -> list:
    """Standard 0→1 ramp over the full overlap."""
    return [(0, 0.0), (duration_frames, 1.0)]


def dip_midpoint(duration_frames: int) -> int:
    """Frame at which a dip-through-colour transition is pure colour.

    Clamped to ``[1, duration_frames - 1]`` when the overlap is long
    enough, so the two halves never collapse onto the same frame and
    produce duplicate keyframes. For a 1-frame overlap there is no room
    for a midpoint and the caller gets frame 0.
    """
    if duration_frames < 2:
        return 0
    return max(1, min(duration_frames - 1, duration_frames // 2))


def _parse_color(value: Any) -> RgbColor:
    """Coerce *value* to an RGB triple.

    Accepts:
      * 3-tuple/list of floats in [0, 1]
      * 4-tuple (alpha discarded)
      * "#rrggbb" or "#rgb" hex string
      * "rrggbb" or "rgb" (no hash) hex string
    Falls back to :data:`DEFAULT_DIP_COLOR` when value is None or
    can't be parsed.
    """
    if value is None:
        return DEFAULT_DIP_COLOR
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            r, g, b = float(value[0]), float(value[1]), float(value[2])
            return (
                max(0.0, min(1.0, r)),
                max(0.0, min(1.0, g)),
                max(0.0, min(1.0, b)),
            )
        except (TypeError, ValueError):
            return DEFAULT_DIP_COLOR
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
            return (
                int(s[0] * 2, 16) / 255.0,
                int(s[1] * 2, 16) / 255.0,
                int(s[2] * 2, 16) / 255.0,
            )
        if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
            return (
                int(s[0:2], 16) / 255.0,
                int(s[2:4], 16) / 255.0,
                int(s[4:6], 16) / 255.0,
            )
    return DEFAULT_DIP_COLOR


# --------------------------------------------------------------------------- #
# Plain dissolve / cross_fade
# --------------------------------------------------------------------------- #

class _BasicDissolve(Transition):
    """Shared implementation for kinds that differ only in composite mode."""

    COMPOSITE: str = "normal"

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty_plan(self.KIND)
        incoming = ClipPlan(
            blend=_fade_in_keys(duration_frames),
            composite_mode=self.COMPOSITE,
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )


@register("dissolve")
class Dissolve(_BasicDissolve):
    """Plain alpha cross-dissolve (V1 stays visible, V2 fades in over)."""


@register("cross_fade")
class CrossFade(_BasicDissolve):
    """Movie-Maker label for the standard dissolve. Identical behaviour."""


@register("additive_dissolve")
class AdditiveDissolve(_BasicDissolve):
    """Dissolve with additive blending — bright areas sum together briefly."""

    COMPOSITE = "add"


@register("non_additive_dissolve")
class NonAdditiveDissolve(_BasicDissolve):
    """Dissolve with max-blend — keeps the brighter pixel of the two clips."""

    COMPOSITE = "non_add"


# --------------------------------------------------------------------------- #
# Blur dissolve
# --------------------------------------------------------------------------- #

@register("blur_dissolve")
class BlurDissolve(Transition):
    """Cross-dissolve plus a blur that peaks at mid-transition.

    The outgoing clip's blur ramps ``0 → peak`` over the first half;
    the incoming clip's blur starts at ``peak`` and ramps to 0 over the
    second half. The two halves overlap at the midpoint where both
    clips are at peak blur, producing the classic "out-of-focus
    cross-fade" look.

    ``params["peak_size"]`` overrides the blur peak (Fusion Blur Size
    units; default :data:`DEFAULT_BLUR_DISSOLVE_PEAK_SIZE`).
    """

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty_plan(self.KIND)
        params = params or {}
        peak = float(params.get("peak_size", DEFAULT_BLUR_DISSOLVE_PEAK_SIZE))

        incoming = ClipPlan(
            blend=_fade_in_keys(duration_frames),
            blur_size=[(0, peak), (duration_frames, 0.0)],
        )
        outgoing = ClipPlan(
            blur_size=[(0, 0.0), (duration_frames, peak)],
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
            outgoing=outgoing,
        )


# --------------------------------------------------------------------------- #
# Dip to colour
# --------------------------------------------------------------------------- #

@register("dip_to_color")
class DipToColor(Transition):
    """The transition dips through a solid colour at the midpoint.

    All of the work happens on the clip that ends up on the **upper**
    track, because only that clip can both cover the one below (with the
    colour) and then get out of its way. Two blends drive it:

    * ``color_blend`` — the clip's image over the solid colour. Held at
      ``0`` (pure colour) for the first half, then ramping to ``1``.
    * ``blend``       — the whole thing's opacity over the clip below.
      Ramps ``0 → 1`` across the first half, then holds.

    Together: transparent at frame 0 (the outgoing clip shows through),
    opaque colour at the midpoint, the incoming image by the end.

    ``params["color"]`` accepts an RGB triple in ``[0, 1]`` or a
    ``"#rrggbb"`` hex string. Defaults to black.
    """

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty_plan(self.KIND)
        params = params or {}
        color = _parse_color(params.get("color"))
        mid = dip_midpoint(duration_frames)
        if mid <= 0:
            # Too short to dip through anything; degrade to a cross-fade.
            return TransitionPlan(
                kind=self.KIND,
                duration_frames=duration_frames,
                incoming=ClipPlan(blend=_fade_in_keys(duration_frames)),
            )

        incoming = ClipPlan(
            background_color=color,
            blend=[(0, 0.0), (mid, 1.0)],
            color_blend=[(0, 0.0), (mid, 0.0), (duration_frames, 1.0)],
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )


__all__ = [
    "AdditiveDissolve",
    "BlurDissolve",
    "CrossFade",
    "DipToColor",
    "Dissolve",
    "NonAdditiveDissolve",
]
