"""Fade-family transitions.

These all dip through a solid colour rather than crossing the two clips
directly:

* :class:`Fade` — Movie Maker's plain fade; equivalent to dipping
  through black.
* :class:`FadeThroughGray` — same shape, mid-grey instead of black.
* :class:`FadeThroughWhite` — same shape, white. Reads as a camera
  flash or a lightbox rather than a fade-out, and generally sits better
  against bright holiday photographs than grey does.
* :class:`BlurThroughBlack` — fades through black while also blurring
  the outgoing clip out of focus and the incoming clip into focus.

All four are layered on top of :class:`~slideshow.transitions.dissolves.DipToColor`
so any improvements to dip-to-colour timing flow into the fades for free.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ClipPlan, Transition, TransitionPlan, register
from .dissolves import (
    DEFAULT_BLUR_DISSOLVE_PEAK_SIZE,
    DipToColor,
    _empty_plan,
    dip_midpoint,
)


class _DipPreset(Transition):
    """A dip-to-colour with the colour fixed by the subclass."""

    COLOR = (0.0, 0.0, 0.0)

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty_plan(self.KIND)
        plan = DipToColor().plan(
            duration_frames,
            params={"color": self.COLOR},
            fps=fps,
        )
        # Preserve our own kind label on the returned plan.
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=plan.duration_frames,
            incoming=plan.incoming,
            outgoing=plan.outgoing,
        )


@register("fade")
class Fade(_DipPreset):
    """Movie Maker "Fade" — dip through black."""

    COLOR = (0.0, 0.0, 0.0)


@register("fade_through_gray")
class FadeThroughGray(_DipPreset):
    """Movie Maker "Fade through gray" — dip through 50% grey."""

    COLOR = (0.5, 0.5, 0.5)


@register("fade_through_white")
class FadeThroughWhite(_DipPreset):
    """Dip through white — a flash rather than a fade."""

    COLOR = (1.0, 1.0, 1.0)


@register("blur_through_black")
class BlurThroughBlack(Transition):
    """Movie Maker "Blur through black" — dip-to-black plus heavy blur.

    The two-clip dip-to-colour timing is reused; we additionally animate
    the blur on each clip so the outgoing clip blurs into the black at
    the midpoint and the incoming clip un-blurs out of it.
    ``params["peak_size"]`` overrides the peak blur amount.
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
        mid = dip_midpoint(duration_frames)

        base = DipToColor().plan(
            duration_frames,
            params={"color": (0.0, 0.0, 0.0)},
            fps=fps,
        )

        # The dip itself rides entirely on the incoming (upper) clip; the
        # outgoing clip is still visible through the first half, so its
        # blur is what sells the "blur into black" half of the move.
        incoming = ClipPlan(
            background_color=base.incoming.background_color,
            blend=base.incoming.blend,
            color_blend=base.incoming.color_blend,
            blur_size=[(mid, peak), (duration_frames, 0.0)],
        )
        outgoing = ClipPlan(
            blur_size=[(0, 0.0), (mid, peak)],
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
            outgoing=outgoing,
        )


__all__ = ["BlurThroughBlack", "Fade", "FadeThroughGray", "FadeThroughWhite"]
