"""Geometry-family transitions: slides, pushes, zooms.

All Fusion coordinates use the normalised image space documented in
:mod:`slideshow.transitions.base`:

* ``(0.5, 0.5)`` — centre of the visible frame
* ``(-0.5, 0.5)`` — fully off-screen to the left
* ``( 1.5, 0.5)`` — fully off-screen to the right
* ``(0.5, -0.5)`` — fully off-screen below
* ``(0.5,  1.5)`` — fully off-screen above

Direction names follow Movie Maker's convention: the kind suffix names
the *direction the incoming clip travels*, not the side it enters
from. So ``slide_left`` enters from the right and moves leftward to
settle in centre frame.

Slide vs. push:

* **slide_***: only the incoming clip moves. The outgoing clip stays
  put and is covered up.
* **push_***: both clips travel together — the outgoing clip exits the
  opposite side while the incoming clip enters from the matching side.

Zoom-in / zoom-out:

* **zoom_in**: the incoming clip grows from a tiny dot in the centre
  to full size while fading in over the outgoing clip.
* **zoom_out**: the incoming clip starts huge (oversized) and shrinks
  down to full size while fading in over the outgoing clip.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..fusion_comps import TransformAnimation
from .base import ClipPlan, Transition, TransitionPlan, register


# Off-screen anchors keyed by direction-of-motion.
# Each value is (off_screen_start, on_screen_end) for the *incoming* clip.
_SLIDE_ENDPOINTS: Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]] = {
    "left":   ((1.5, 0.5), (0.5, 0.5)),   # enter from right, move left
    "right":  ((-0.5, 0.5), (0.5, 0.5)),  # enter from left,  move right
    "top":    ((0.5, -0.5), (0.5, 0.5)),  # enter from below, move up
    "bottom": ((0.5, 1.5), (0.5, 0.5)),   # enter from above, move down
}

# Where the *outgoing* clip needs to be by end-of-transition for a push
# (it leaves through the opposite edge from where the incoming entered).
_PUSH_OUTGOING_EXIT: Dict[str, Tuple[float, float]] = {
    "left":   (-0.5, 0.5),
    "right":  (1.5, 0.5),
    "top":    (0.5, 1.5),
    "bottom": (0.5, -0.5),
}


def _empty(kind: str) -> TransitionPlan:
    return TransitionPlan(kind=kind, duration_frames=0)


# --------------------------------------------------------------------------- #
# Slide
# --------------------------------------------------------------------------- #

class _SlideBase(Transition):
    """Shared planner for the four slide kinds."""

    DIRECTION: str = ""

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty(self.KIND)
        start_pt, end_pt = _SLIDE_ENDPOINTS[self.DIRECTION]
        incoming = ClipPlan(
            transform=TransformAnimation(
                center=[(0, start_pt), (duration_frames, end_pt)],
            ),
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )

    def mirror(self, plan: TransitionPlan) -> TransitionPlan:
        """Slide the *outgoing* clip away instead, in the same direction.

        The default time-reversal would send the outgoing clip back out
        the side the incoming clip was supposed to enter from, so
        ``slide_left`` would travel rightward on every other transition.
        Continuing off the far edge instead — a point reflection of the
        entry anchor through centre frame — keeps the named direction.
        """
        duration = plan.duration_frames
        if duration <= 0:
            return plan
        start_pt, end_pt = _SLIDE_ENDPOINTS[self.DIRECTION]
        exit_pt = (
            2.0 * end_pt[0] - start_pt[0],
            2.0 * end_pt[1] - start_pt[1],
        )
        return TransitionPlan(
            kind=plan.kind,
            duration_frames=duration,
            incoming=ClipPlan(),
            outgoing=ClipPlan(
                transform=TransformAnimation(
                    center=[(0, end_pt), (duration, exit_pt)],
                ),
            ),
        )


@register("slide_left")
class SlideLeft(_SlideBase):
    """Incoming clip enters from the right and moves leftward to centre."""
    DIRECTION = "left"


@register("slide_right")
class SlideRight(_SlideBase):
    """Incoming clip enters from the left and moves rightward to centre."""
    DIRECTION = "right"


@register("slide_top")
class SlideTop(_SlideBase):
    """Incoming clip enters from below and moves upward to centre."""
    DIRECTION = "top"


@register("slide_bottom")
class SlideBottom(_SlideBase):
    """Incoming clip enters from above and moves downward to centre."""
    DIRECTION = "bottom"


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #

class _PushBase(Transition):
    """Shared planner for the four push kinds."""

    DIRECTION: str = ""

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty(self.KIND)
        start_pt, end_pt = _SLIDE_ENDPOINTS[self.DIRECTION]
        outgoing_exit = _PUSH_OUTGOING_EXIT[self.DIRECTION]

        incoming = ClipPlan(
            transform=TransformAnimation(
                center=[(0, start_pt), (duration_frames, end_pt)],
            ),
        )
        outgoing = ClipPlan(
            transform=TransformAnimation(
                center=[(0, (0.5, 0.5)), (duration_frames, outgoing_exit)],
            ),
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
            outgoing=outgoing,
        )

    def mirror(self, plan: TransitionPlan) -> TransitionPlan:
        """Pushes are stacking-order agnostic, so mirror to themselves.

        The two clips travel together and stay exactly edge-to-edge —
        they never overlap on screen — so it makes no difference which
        one is on the upper track.
        """
        return plan


@register("push_left")
class PushLeft(_PushBase):
    """Both clips travel leftward; outgoing exits left, incoming enters from right."""
    DIRECTION = "left"


@register("push_right")
class PushRight(_PushBase):
    """Both clips travel rightward; outgoing exits right, incoming enters from left."""
    DIRECTION = "right"


@register("push_top")
class PushTop(_PushBase):
    """Both clips travel upward; outgoing exits top, incoming enters from below."""
    DIRECTION = "top"


@register("push_bottom")
class PushBottom(_PushBase):
    """Both clips travel downward; outgoing exits bottom, incoming enters from above."""
    DIRECTION = "bottom"


# --------------------------------------------------------------------------- #
# Zoom
# --------------------------------------------------------------------------- #

DEFAULT_ZOOM_OUT_START_SIZE = 3.0  # incoming starts 3× oversized for zoom_out
DEFAULT_ZOOM_IN_START_SIZE = 0.0   # incoming starts at zero size for zoom_in


@register("zoom_in")
class ZoomIn(Transition):
    """Incoming clip grows from nothing to full size, fading in over outgoing.

    ``params["start_size"]`` overrides the starting size (default 0.0 —
    a single pixel at the centre).
    """

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty(self.KIND)
        params = params or {}
        start = float(params.get("start_size", DEFAULT_ZOOM_IN_START_SIZE))
        incoming = ClipPlan(
            transform=TransformAnimation(
                size=[(0, start), (duration_frames, 1.0)],
            ),
            blend=[(0, 0.0), (duration_frames, 1.0)],
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )


@register("zoom_out")
class ZoomOut(Transition):
    """Incoming clip starts oversized and shrinks to full size while fading in.

    ``params["start_size"]`` overrides the starting size (default 3.0 —
    cropped to the centre third of the incoming frame initially).
    """

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return _empty(self.KIND)
        params = params or {}
        start = float(params.get("start_size", DEFAULT_ZOOM_OUT_START_SIZE))
        incoming = ClipPlan(
            transform=TransformAnimation(
                size=[(0, start), (duration_frames, 1.0)],
            ),
            blend=[(0, 0.0), (duration_frames, 1.0)],
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )


__all__ = [
    "PushBottom",
    "PushLeft",
    "PushRight",
    "PushTop",
    "SlideBottom",
    "SlideLeft",
    "SlideRight",
    "SlideTop",
    "ZoomIn",
    "ZoomOut",
]
