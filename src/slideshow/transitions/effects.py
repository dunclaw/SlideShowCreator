"""Effects-family transitions: 'none', 'pixelate', 'smooth_cut'.

* :class:`Nothing` — registered for ``"none"``; produces an empty plan
  so callers can treat every transition uniformly without branching on
  the kind. Equivalent to a hard cut.
* :class:`Pixelate` — both clips pixelate up to a peak block size at
  the midpoint while the incoming clip simultaneously fades in.
* :class:`SmoothCut` — stub. Real smooth-cut requires optical-flow
  retiming Resolve only exposes via its native transition. The plan
  here is a very short alpha dissolve (capped at 3 frames) so the
  applier still produces a watchable result.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ClipPlan, Transition, TransitionPlan, register


DEFAULT_PIXELATE_PEAK_SIZE = 60.0  # Fusion Pixelate 'Size' (pixel block edge)
SMOOTH_CUT_MAX_FRAMES = 3


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
    """Both clips pixelate to a peak block size at the midpoint.

    The incoming clip also fades in across the overlap, so the second
    half reveals the new image as the blocks resolve back to pixels.
    ``params["peak_size"]`` overrides the peak block edge length.
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
        mid = duration_frames // 2

        outgoing = ClipPlan(
            pixelate_size=[(0, 1.0), (mid, peak)],
        )
        incoming = ClipPlan(
            blend=[(0, 0.0), (duration_frames, 1.0)],
            pixelate_size=[(mid, peak), (duration_frames, 1.0)],
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
