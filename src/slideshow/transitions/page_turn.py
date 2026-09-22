"""Page turns — 3D rotations about a vertical edge, like turning a page.

Two transitions live here. Both are genuine 3D rotations, not the in-plane
spin :mod:`flip` does: the image becomes a texture on a ``Shape3D`` plane,
hinged on one vertical edge, and rotates about the Y axis until it lies flat
and fills the frame exactly. See
:func:`slideshow.fusion_comps.build_page_turn_graph` for the node graph and
the camera fit that makes "flat" mean "pixel-identical to the untouched
photo".

Two directions
--------------

:class:`PageTurn` moves the **incoming** photo: it arrives from off to the
right, rotates down onto the stack and comes to rest — the page you are
turning *to* dropping into place. Hinged right, so it sweeps right-to-left,
the way a page turns in a book read left-to-right.

:class:`PageTurnAway` moves the **outgoing** photo instead: it lifts off and
sweeps away to the left, uncovering the next photo underneath. That needs the
outgoing clip on the upper track, which it asks for via
:attr:`~slideshow.transitions.base.Transition.PREFERS_OUTGOING_ON_TOP`; it is
otherwise the same plan, mirrored in time by ``plan_transition``. Hinged
left, so the free right edge is the one that lifts.

Angles
------

``0°`` is flat-on and fills the frame. Positive angles tip the free edge
**toward** the camera, which reads as a page being lowered onto a pile;
negative angles tip it away, which reads as the page swinging up from below.
Positive is the default for that reason.

That statement is about the *free* edge, not about the raw rotation, so the
sign flips with the hinge: see :meth:`PageTurn.plan`. A left-hinged page given
the same raw angle as a right-hinged one would sink into the screen instead of
lifting off it.

The sweep starts a little past 90° so the page begins fractionally behind
edge-on. With back-face culling on it is invisible until it crosses 90°,
which hides the pop-in and costs only a frame or two.

``params``
----------

* ``hinge`` — ``"right"`` or ``"left"``; defaults per transition.
* ``angle`` — start angle in degrees (default :data:`PAGE_START_ANGLE`).
* ``focal_length`` — camera focal length in mm. Shorter exaggerates the
  fold. Default :data:`slideshow.fusion_comps.DEFAULT_PAGE_FOCAL_LENGTH`.
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional

from ..fusion_comps import (
    DEFAULT_PAGE_FOCAL_LENGTH,
    PAGE_HINGES,
    PageTurnAnimation,
    ScalarKeyframe,
)
from .base import ClipPlan, Transition, TransitionPlan, register


#: Angle, in degrees, the page starts at. Slightly past edge-on so the page
#: is still hidden by back-face culling on the first frame.
PAGE_START_ANGLE = 100.0

#: Fractions of the overlap mapped to fractions of the start angle. Front
#: loaded: the page covers most of its arc early and then settles, which is
#: what "let go of a page" looks like. A linear sweep reads mechanical, and
#: easing *into* the motion instead makes it look like it snags.
PAGE_PROFILE = (
    (0.0, 1.0),
    (0.25, 0.86),
    (0.55, 0.45),
    (0.80, 0.12),
    (1.0, 0.0),
)


def page_angle_keys(duration_frames: int, start_angle: float) -> List[ScalarKeyframe]:
    """Keyframes sweeping from *start_angle* to flat over visible frames.

    Fractions are snapped to strictly increasing integer frames; points that
    collide after rounding are dropped so a very short overlap degenerates
    gracefully to a straight sweep rather than emitting duplicate keys.
    A clip of length ``duration_frames`` renders local frames
    ``0..duration_frames - 1``. The final key therefore lands on that last
    visible frame, not at ``duration_frames`` (which is already the first
    frame of the adjoining body segment). The page has to come to rest
    precisely flat while it is still visible, or its residual angle produces
    a crop-and-position snap at the cut.
    """
    last_frame = max(0, duration_frames - 1)
    if last_frame == 0:
        return [(0, 0.0)]

    keys: List[ScalarKeyframe] = []
    for fraction, scale in PAGE_PROFILE:
        frame = int(round(fraction * last_frame))
        if keys and frame <= keys[-1][0]:
            continue
        if frame >= last_frame:
            break
        keys.append((frame, start_angle * scale))
    if not keys:
        keys.append((0, start_angle))
    keys.append((last_frame, 0.0))
    return keys


@register("page_turn")
class PageTurn(Transition):
    """Incoming photo rotates in about a vertical edge and lands flat."""

    #: Which edge the page pivots on when ``params`` doesn't say. Hinging on
    #: the right brings the incoming page in from the right, like the page
    #: you are turning *to* dropping into place.
    DEFAULT_HINGE: ClassVar[str] = "right"

    #: A physical page turn has a natural pace — too fast and the fold never
    #: reads, too slow and it stops looking like a hand turning a page. It
    #: gets a little more room than a dissolve, but not a lot.
    MAX_DURATION_FRAMES = 60

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

        hinge = str(params.get("hinge", self.DEFAULT_HINGE)).lower()
        if hinge not in PAGE_HINGES:
            hinge = self.DEFAULT_HINGE
        try:
            start_angle = float(params.get("angle", PAGE_START_ANGLE))
        except (TypeError, ValueError):
            start_angle = PAGE_START_ANGLE
        try:
            focal_length = float(
                params.get("focal_length", DEFAULT_PAGE_FOCAL_LENGTH)
            )
        except (TypeError, ValueError):
            focal_length = DEFAULT_PAGE_FOCAL_LENGTH
        if focal_length <= 0:
            focal_length = DEFAULT_PAGE_FOCAL_LENGTH

        # Rotating about Y sends a point at pivot-relative ``x`` to
        # ``z' = -x·sin(angle)``, and the camera is at +Z. A right hinge puts
        # the free edge at negative x, so a positive angle lifts it toward the
        # camera; a left hinge puts it at positive x, where the same angle
        # would push it *into* the screen instead. Mirroring the sign with the
        # hinge keeps "positive tips the free edge toward the camera" true for
        # both, so the two directions are mirror images and not two different
        # motions.
        if hinge == "left":
            start_angle = -start_angle

        incoming = ClipPlan(
            page_turn=PageTurnAnimation(
                angle=page_angle_keys(duration_frames, start_angle),
                hinge=hinge,
                focal_length=focal_length,
            )
        )
        # The outgoing photo just sits there and gets covered up, exactly as
        # the page under the one being turned would.
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )


@register("page_turn_away")
class PageTurnAway(PageTurn):
    """Outgoing photo peels away, revealing the next one underneath.

    The other reading of a page turn, and the one that needs the *outgoing*
    slide on the upper track — a page lifting off the stack has to be above
    the photo it uncovers. :attr:`PREFERS_OUTGOING_ON_TOP` tells the layout
    to lift it, and :func:`~slideshow.transitions.base.plan_transition` then
    mirrors this plan so the rotation lands on the clip that moves.

    Planned identically to :class:`PageTurn` and mirrored into place, rather
    than written out backwards here, so the two stay in step: any change to
    the arc, the lens or the resting angle applies to both.
    """

    PREFERS_OUTGOING_ON_TOP = True

    #: Hinged on the *left*, so the free right edge lifts and sweeps left
    #: over the spine — a page being turned right-to-left, the way a book is
    #: read. (:class:`PageTurn` hinges right because it is the page landing,
    #: not the page leaving.)
    DEFAULT_HINGE = "left"


__all__ = [
    "PAGE_PROFILE",
    "PAGE_START_ANGLE",
    "PageTurn",
    "PageTurnAway",
    "page_angle_keys",
]
