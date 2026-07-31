"""Drop transition — incoming clip falls in from above with a bounce.

The incoming clip's Center starts above the frame and animates down to
centre, slightly overshooting and settling. The outgoing clip stays
put underneath until the incoming clip covers it.

Keyframe sequence for the incoming clip's Y:

* frame 0                    — Y = 1.5    (fully above the frame)
* frame ~70%                 — Y = 0.35   (overshoot below centre)
* frame ~85%                 — Y = 0.55   (bounce back above centre)
* frame ``duration_frames``  — Y = 0.5    (settle at centre)

X is fixed at 0.5 throughout. The exact percentages produce a single
visible bounce; a future refinement could expose ``params["bounces"]``
to chain multiple overshoots.

A short blend ramp at the very start ensures the incoming clip's
alpha is solid by the time it's visible — without it, very short
overlaps could pop in unexpectedly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..fusion_comps import TransformAnimation
from .base import ClipPlan, Transition, TransitionPlan, register


@register("drop")
class Drop(Transition):
    """Incoming clip drops in from above with a single bounce-and-settle."""

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return TransitionPlan(kind=self.KIND, duration_frames=0)

        # Bounce timing as fractions of total overlap, then snapped to
        # frame integers. For very short durations the fractions may
        # collide; we de-duplicate while preserving order so SetKeyFrames
        # always sees strictly increasing times.
        f_overshoot = max(1, int(round(duration_frames * 0.70)))
        f_bounce = max(f_overshoot + 1, int(round(duration_frames * 0.85)))
        f_end = max(f_bounce + 1, duration_frames)

        center_keys = [
            (0, (0.5, 1.5)),
            (f_overshoot, (0.5, 0.35)),
            (f_bounce, (0.5, 0.55)),
            (f_end, (0.5, 0.5)),
        ]
        incoming = ClipPlan(
            transform=TransformAnimation(center=center_keys),
        )
        return TransitionPlan(
            kind=self.KIND,
            duration_frames=duration_frames,
            incoming=incoming,
        )

    def mirror(self, plan: TransitionPlan) -> TransitionPlan:
        """Drop the *outgoing* clip out of the bottom of frame instead.

        Time-reversing the incoming path would fling the outgoing clip
        back up through the top, which reads as a "rise", not a drop. We
        mirror the bounce vertically about centre frame so the clip
        wobbles once and then falls away downwards, uncovering the
        incoming clip on the track below.
        """
        duration = plan.duration_frames
        if duration <= 0:
            return plan
        f_wobble = max(1, int(round(duration * 0.15)))
        f_settle = max(f_wobble + 1, int(round(duration * 0.30)))
        center_keys = [
            (0, (0.5, 0.5)),
            (f_wobble, (0.5, 0.55)),
            (f_settle, (0.5, 0.45)),
            (max(f_settle + 1, duration), (0.5, -0.5)),
        ]
        return TransitionPlan(
            kind=plan.kind,
            duration_frames=duration,
            incoming=ClipPlan(),
            outgoing=ClipPlan(
                transform=TransformAnimation(center=center_keys),
            ),
        )


__all__ = ["Drop"]
