"""Page turn — the incoming photo swings in like a page being laid down.

This is a genuine 3D rotation, not the in-plane spin :mod:`flip` does. The
incoming image becomes a texture on a ``Shape3D`` plane, hinged on one
vertical edge, and rotates about the Y axis until it lies flat and fills the
frame exactly. See :func:`slideshow.fusion_comps.build_page_turn_graph` for
the node graph and the camera fit that makes "flat" mean "pixel-identical to
the untouched photo".

Direction
---------

The page hinges on its **right** edge by default, so it sweeps right-to-left
— the way a page turns in a book read left-to-right.

It is the *incoming* page that moves: it arrives from off to the right,
rotates down onto the stack and comes to rest. The other reading — the
outgoing page peeling away to reveal the next photo underneath — needs the
outgoing clip on the upper track, which the split-track layout never
produces, so it is a separate piece of work rather than a parameter here.

Angles
------

``0°`` is flat-on and fills the frame. Positive angles tip the free (left)
edge **toward** the camera, which reads as a page being lowered onto a pile;
negative angles tip it away, which reads as the page swinging up from below.
Positive is the default for that reason.

The sweep starts a little past 90° so the page begins fractionally behind
edge-on. With back-face culling on it is invisible until it crosses 90°,
which hides the pop-in and costs only a frame or two.

``params``
----------

* ``hinge`` — ``"right"`` (default) or ``"left"``.
* ``angle`` — start angle in degrees (default :data:`PAGE_START_ANGLE`).
* ``focal_length`` — camera focal length in mm. Shorter exaggerates the
  fold. Default :data:`slideshow.fusion_comps.DEFAULT_PAGE_FOCAL_LENGTH`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    """Keyframes sweeping from *start_angle* to flat over *duration_frames*.

    Fractions are snapped to strictly increasing integer frames; points that
    collide after rounding are dropped so a very short overlap degenerates
    gracefully to a straight sweep rather than emitting duplicate keys.
    The final key is always exactly ``(duration_frames, 0.0)`` — the page has
    to come to rest *precisely* flat, because the next frame is the body
    segment showing the untouched photo and any residual angle shows up as a
    visible jump at the cut.
    """
    keys: List[ScalarKeyframe] = []
    for fraction, scale in PAGE_PROFILE:
        frame = int(round(fraction * duration_frames))
        if keys and frame <= keys[-1][0]:
            continue
        if frame >= duration_frames:
            break
        keys.append((frame, start_angle * scale))
    if not keys:
        keys.append((0, start_angle))
    keys.append((duration_frames, 0.0))
    return keys


@register("page_turn")
class PageTurn(Transition):
    """Incoming photo rotates in about a vertical edge and lands flat."""

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

        hinge = str(params.get("hinge", "right")).lower()
        if hinge not in PAGE_HINGES:
            hinge = "right"
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


__all__ = ["PAGE_PROFILE", "PAGE_START_ANGLE", "PageTurn", "page_angle_keys"]
