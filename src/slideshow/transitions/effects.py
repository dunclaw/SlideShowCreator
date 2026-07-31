"""Effects-family transitions: 'none', 'pixelate', 'smooth_cut'.

* :class:`Nothing` — registered for ``"none"``; produces an empty plan
  so callers can treat every transition uniformly without branching on
  the kind. Equivalent to a hard cut.
* :class:`Pixelate` — both clips pixelate up to a peak block size,
  **hold** there while the images swap, then resolve back. The hold is
  the point: ramping straight through the peak makes the effect flash
  past too quickly to register.
* :class:`SmoothCut` — stub. Real smooth-cut requires optical-flow
  retiming Resolve only exposes via its native transition. The plan
  here is a very short alpha dissolve (capped at 3 frames) so the
  applier still produces a watchable result.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ClipPlan, Transition, TransitionPlan, register


DEFAULT_PIXELATE_PEAK_SIZE = 96.0  # Fusion Pixelate 'Size' (pixel block edge)
#: Fraction of the overlap spent fully pixelated, split either side of
#: the midpoint. The images swap inside this window, where both are
#: unrecognisable blocks and the cut cannot be seen.
PIXELATE_HOLD_FRACTION = 0.34
SMOOTH_CUT_MAX_FRAMES = 3


def _ramp(f0, v0, f1, v1, f2, v2):
    """Three keyframes with duplicate times collapsed.

    Fusion needs strictly increasing keyframe times, and a hold window
    can legitimately shrink to nothing on a very short overlap. When two
    frames coincide the *later* value wins, so the curve still ends where
    it should.
    """
    keys = []
    for frame, value in ((f0, v0), (f1, v1), (f2, v2)):
        frame = int(frame)
        if keys and keys[-1][0] == frame:
            keys[-1] = (frame, value)
        else:
            keys.append((frame, value))
    return keys


@register("none")
class Nothing(Transition):
    """Hard cut. Returns an empty plan regardless of requested duration."""

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        return TransitionPlan(kind=self.KIND, duration_frames=0)


@register("pixelate")
class Pixelate(Transition):
    """Both clips pixelate to a peak block size and hold at the midpoint.

    The outgoing clip coarsens up to the peak and stays there; the
    incoming clip holds at the peak and then resolves. The swap between
    them happens inside the hold, so it lands while the frame is nothing
    but blocks.

    ``params["peak_size"]`` overrides the peak block edge length and
    ``params["hold"]`` the fraction of the overlap spent at that peak.
    """

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return TransitionPlan(kind=self.KIND, duration_frames=0)
        params = params or {}
        peak = float(params.get("peak_size", DEFAULT_PIXELATE_PEAK_SIZE))
        hold = float(params.get("hold", PIXELATE_HOLD_FRACTION))
        mid = duration_frames // 2

        # Half the hold either side of the midpoint, clamped so both the
        # ramp up and the ramp down keep at least one frame.
        half_hold = int(round(duration_frames * max(0.0, hold) / 2.0))
        half_hold = max(0, min(half_hold, mid - 1, duration_frames - mid - 1))
        hold_start = mid - half_hold
        hold_end = mid + half_hold

        outgoing = ClipPlan(
            pixelate_size=_ramp(0, 1.0, hold_start, peak, duration_frames, peak),
        )
        incoming = ClipPlan(
            # Swap inside the hold rather than crossfading across the
            # whole overlap — a long crossfade of two pixelated images is
            # just mud, and hides the blocks we went to the trouble of
            # making.
            blend=_ramp(0, 0.0, hold_start, 0.0, hold_end, 1.0),
            pixelate_size=_ramp(0, peak, hold_end, peak, duration_frames, 1.0),
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
            outgoing=outgoing,
        )


@register("smooth_cut")
class SmoothCut(Transition):
    """Stub: short alpha dissolve in lieu of real optical-flow smooth-cut.

    Capped at :data:`SMOOTH_CUT_MAX_FRAMES` frames so callers can pass
    a long requested duration without getting a slow dissolve they
    didn't ask for. A future phase may replace this with a real
    optical-flow implementation backed by Resolve's native transition.
    """

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return TransitionPlan(kind=self.KIND, duration_frames=0)
        clamped = min(duration_frames, SMOOTH_CUT_MAX_FRAMES)
        incoming = ClipPlan(blend=[(0, 0.0), (clamped, 1.0)])
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=clamped,
            incoming=incoming,
        )


__all__ = ["Nothing", "Pixelate", "SmoothCut"]
