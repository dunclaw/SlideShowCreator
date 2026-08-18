"""Drop transition — incoming clip falls in from above and bounces.

The incoming clip's Center starts above the frame, accelerates downward,
lands at centre and bounces twice before settling. The outgoing clip
stays put underneath until the incoming clip covers it.

Keyframe sequence for the incoming clip's Y (fractions of the overlap):

* 0%    — Y = 1.5    fully above the frame, at rest
* 18%   — Y = 1.40   barely moved; gravity is still building
* 38%   — Y = 1.12
* 60%   — Y = 0.5    impact, dead centre
* 74%   — Y = 0.70   first bounce
* 86%   — Y = 0.5    back down
* 94%   — Y = 0.55   second, much smaller bounce
* 100%  — Y = 0.5    settled

The shape matters more than the numbers. Spending most of the fall in
the first third of the distance is what reads as *gravity*; an evenly
paced descent followed by a small late wobble reads as a glitch. The
bounce goes **up** after impact rather than overshooting below centre —
overshooting below is a spring, not a falling object — and the second
bounce is roughly a third of the first so the decay is visible.

X is fixed at 0.5 throughout. A future refinement could expose
``params["bounces"]`` to chain more overshoots, and a small rotation
would sell the "thrown photograph" feel.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..fusion_comps import TransformAnimation
from .base import ClipPlan, Transition, TransitionPlan, register


#: ``(fraction of duration, Y)`` for the fall-and-settle. See module docs.
DROP_PROFILE: Tuple[Tuple[float, float], ...] = (
    (0.00, 1.50),
    (0.18, 1.40),
    (0.38, 1.12),
    (0.60, 0.50),
    (0.74, 0.70),
    (0.86, 0.50),
    (0.94, 0.55),
    (1.00, 0.50),
)


def _profile_keys(
    duration_frames: int, profile: Tuple[Tuple[float, float], ...]
) -> List[Tuple[int, Tuple[float, float]]]:
    """Snap a ``(fraction, y)`` profile onto integer frames.

    Fractions collide on short overlaps, and Fusion needs strictly
    increasing keyframe times, so later points are pushed forward and any
    that no longer fit inside the overlap are dropped. The final resting
    position is always kept, on the last frame, so the clip can never end
    mid-fall.
    """
    keys: List[Tuple[int, Tuple[float, float]]] = []
    for fraction, y in profile[:-1]:
        frame = int(round(duration_frames * fraction))
        if keys:
            frame = max(frame, keys[-1][0] + 1)
        if frame >= duration_frames:
            break
        keys.append((frame, (0.5, y)))
    keys.append((duration_frames, (0.5, profile[-1][1])))
    return keys


@register("drop")
class Drop(Transition):
    """Incoming clip accelerates in from above, lands and bounces twice.

    The gravity curve spends most of its length in the first third of the
    fall, and the two bounces are packed into the tail — so the same frame
    count reads as much shorter here than it does for a dissolve. It gets a
    higher ceiling for that reason.
    """

    MAX_DURATION_FRAMES = 72

    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        if duration_frames <= 0:
            return TransitionPlan(kind=self.KIND, duration_frames=0)

        incoming = ClipPlan(
            transform=TransformAnimation(
                center=_profile_keys(duration_frames, DROP_PROFILE),
            ),
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
