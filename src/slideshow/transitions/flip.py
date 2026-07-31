"""Flip transition — outgoing clip flips out, incoming clip flips in.

We approximate Movie-Maker's "flip" as a 2D card flip by animating the
``Angle`` input of each clip's Transform plus a hand-off in opacity at
the midpoint:

* Outgoing clip: angle ``0° → 90°`` over the first half, fading out
  across a short window centred on the midpoint.
* Incoming clip: angle ``-90° → 0°`` over the second half, fading in
  across the same window.

The result is the outgoing clip rotating away to an edge, then the
incoming clip rotating in from the opposite edge. A true 3D flip would
require a Fusion 3D scene; this 2D version is the practical equivalent
the existing Transform-only Fusion graph can produce.

The hand-off used to be a single-frame swap, which read as a glitch —
the two images are at different angles either side of it, so the cut is
plainly visible. :data:`FLIP_SWAP_FRACTION` spreads it over a few
frames instead, short enough to still feel like a flip rather than a
dissolve.

``params["axis"]`` (``"horizontal"`` default or ``"vertical"``)
chooses whether the flip is around the vertical axis (image swings
side-to-side) or the horizontal axis (image swings top-to-bottom). The
distinction only affects what the half-flipped frame looks like; we
encode it as direction of the angle sign.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..fusion_comps import TransformAnimation
from .base import ClipPlan, Transition, TransitionPlan, register


#: Length of the mid-flip crossfade, as a fraction of the whole overlap.
FLIP_SWAP_FRACTION = 0.2


@register("flip")
class Flip(Transition):
    """2D card-flip: outgoing rotates 0→90°, incoming rotates -90°→0°."""

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
        axis = str(params.get("axis", "horizontal")).lower()
        # Sign of the rotation; vertical axis (left/right swing) keeps
        # positive degrees, horizontal axis (top/bottom swing) inverts
        # so the image visibly tumbles rather than spins.
        sign = -1.0 if axis == "vertical" else 1.0
        mid = duration_frames // 2

        # Crossfade window around the midpoint, clamped so it always has
        # at least one frame either side and never runs past either end.
        half_swap = max(1, int(round(duration_frames * FLIP_SWAP_FRACTION / 2)))
        swap_start = max(0, mid - half_swap)
        swap_end = min(duration_frames, mid + half_swap)
        if swap_end <= swap_start:
            swap_start, swap_end = 0, duration_frames

        outgoing = ClipPlan(
            transform=TransformAnimation(
                angle=[(0, 0.0), (mid, 90.0 * sign)],
            ),
            blend=[(swap_start, 1.0), (swap_end, 0.0)],
        )
        incoming = ClipPlan(
            transform=TransformAnimation(
                angle=[(mid, -90.0 * sign), (duration_frames, 0.0)],
            ),
            blend=[(swap_start, 0.0), (swap_end, 1.0)],
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
            outgoing=outgoing,
        )


__all__ = ["Flip"]
