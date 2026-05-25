"""Transition framework abstractions.

A *transition* describes how one slide gives way to the next during an
N-frame overlap. This module is pure data + planning logic — it never
touches Resolve. The plans it emits are consumed later by an applier
(separate module) that pokes Fusion comps via ``slideshow.fusion_comps``.

Layout assumption (encoded in plan semantics):

* The outgoing clip lives on V1 and plays through its tail normally.
* The incoming clip lives on V2 and overlaps the outgoing clip's tail
  by ``duration_frames`` frames. The transition's Fusion edits land on
  the *incoming* clip for almost all kinds; a few (push, flip) also
  animate the outgoing clip.

Each transition class implements :meth:`Transition.plan`, returning a
:class:`TransitionPlan` whose ``incoming`` and ``outgoing`` fields are
:class:`ClipPlan` objects. A ClipPlan is a kitchen-sink of optional
effects (transform, blend, blur, pixelate, background, composite mode);
the applier reads the populated fields and assembles the appropriate
Fusion node graph.

Keyframe coordinate system:

* Frame numbers are relative to the **start of the overlap** (frame 0
  = first frame of the transition). The applier converts these to the
  clip-local frame numbers Fusion comps use.
* For Transform geometry, Fusion's normalised coordinates apply:
  ``(0.5, 0.5)`` is the centre of the frame, ``(0, 0)`` is the
  bottom-left corner of the visible area, ``(1, 1)`` is the top-right.
  Values outside ``[0, 1]`` are off-screen.

"None" / "auto":

* ``kind="none"`` is a hard cut and produces an empty plan with
  ``duration_frames=0``.
* ``kind="auto"`` is **not** resolvable here — the auto-mix planner
  must pick a concrete kind first. :func:`plan_transition` raises
  :class:`ValueError` if given ``"auto"``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple, Type

from ..fusion_comps import PointKeyframe, ScalarKeyframe, TransformAnimation
from ..project_model import TransitionChoice


# --------------------------------------------------------------------------- #
# Effect dataclasses
# --------------------------------------------------------------------------- #

#: Acceptable values for :attr:`ClipPlan.composite_mode`. ``"normal"`` is
#: a standard alpha composite; ``"add"`` enables additive blending (used by
#: ``additive_dissolve``); ``"non_add"`` enables max-blend (used by
#: ``non_additive_dissolve``).
COMPOSITE_MODES: frozenset = frozenset({"normal", "add", "non_add"})

#: RGB triple ``(r, g, b)`` with each channel in ``[0, 1]``.
RgbColor = Tuple[float, float, float]


@dataclass
class ClipPlan:
    """Fusion-comp edits to apply to one side of a transition.

    All fields are optional. ``None`` means "leave this aspect alone".

    * ``transform``       — animated Center / Size / Angle / Pivot.
    * ``blend``           — opacity keyframes ``(frame, value)`` with
                            value in ``[0, 1]``. Applied to the
                            Transform's ``Blend`` input (or the Merge's
                            ``Blend`` for additive/non_add modes).
    * ``blur_size``       — Fusion ``Blur`` ``Size`` keyframes. Non-None
                            tells the applier to insert a Blur tool.
    * ``pixelate_size``   — Fusion ``Pixelate`` ``Size`` keyframes.
                            Non-None means insert a Pixelate tool.
    * ``background_color``— RGB triple. Non-None means insert a
                            Background tool of this colour beneath the
                            clip's media so a fade-to-colour transition
                            reveals it as the clip fades out.
    * ``composite_mode``  — how this clip composites onto the *other*
                            side of the transition. Only meaningful on
                            the *incoming* ClipPlan; the outgoing clip
                            is the base layer.
    """

    transform: Optional[TransformAnimation] = None
    blend: Optional[List[ScalarKeyframe]] = None
    blur_size: Optional[List[ScalarKeyframe]] = None
    pixelate_size: Optional[List[ScalarKeyframe]] = None
    background_color: Optional[RgbColor] = None
    composite_mode: str = "normal"

    def __post_init__(self) -> None:
        if self.composite_mode not in COMPOSITE_MODES:
            raise ValueError(
                "ClipPlan.composite_mode must be one of {0}, got {1!r}".format(
                    sorted(COMPOSITE_MODES), self.composite_mode
                )
            )

    def is_empty(self) -> bool:
        """True when this plan has no effects (transform/blend/blur/etc.)."""
        return (
            (self.transform is None or self.transform.is_empty())
            and not self.blend
            and not self.blur_size
            and not self.pixelate_size
            and self.background_color is None
            and self.composite_mode == "normal"
        )


@dataclass
class TransitionPlan:
    """The full plan for one transition: edits for both clips + duration."""

    kind: str
    duration_frames: int
    incoming: ClipPlan = field(default_factory=ClipPlan)
    outgoing: ClipPlan = field(default_factory=ClipPlan)

    @property
    def is_cut(self) -> bool:
        """True when this plan effectively renders as a hard cut."""
        return self.duration_frames <= 0 or (
            self.incoming.is_empty() and self.outgoing.is_empty()
        )


# --------------------------------------------------------------------------- #
# Transition base class + registry
# --------------------------------------------------------------------------- #

#: Module-level registry: kind name -> Transition subclass.
_REGISTRY: Dict[str, Type["Transition"]] = {}


def register(kind: str):
    """Decorator: associate a kind name with a :class:`Transition` subclass.

    Re-registering the same kind raises so silent shadowing can't happen
    when two modules try to claim the same name.
    """
    def deco(cls: Type["Transition"]) -> Type["Transition"]:
        if kind in _REGISTRY:
            raise RuntimeError(
                "Transition kind {0!r} is already registered to {1}".format(
                    kind, _REGISTRY[kind].__name__
                )
            )
        cls.KIND = kind
        _REGISTRY[kind] = cls
        return cls
    return deco


class Transition(abc.ABC):
    """Abstract base for every transition kind.

    Subclasses set :attr:`KIND` via the :func:`register` decorator and
    implement :meth:`plan`. Most subclasses are stateless and act as
    simple plan-builders; nothing prevents an implementation from
    carrying state, but the planner is treated as a pure function.
    """

    KIND: ClassVar[str] = "<abstract>"

    @abc.abstractmethod
    def plan(
        self,
        duration_frames: int,
        *,
        params: Optional[Dict[str, Any]] = None,
        fps: float = 24.0,
    ) -> TransitionPlan:
        """Return a :class:`TransitionPlan` for the given overlap length.

        * ``duration_frames`` — length of the overlap, in frames. Must be
          ``>= 0``. A value of 0 collapses every transition to a cut.
        * ``params`` — optional dict of kind-specific parameters
          (matching ``TransitionChoice.params``); unknown keys must be
          ignored so old projects keep loading on new transition impls.
        * ``fps`` — timeline frame rate, for transitions whose feel
          depends on real-time scale (e.g. ``smooth_cut`` caps itself
          at a few frames regardless of requested duration).
        """
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

def registered_kinds() -> List[str]:
    """Return all transition kinds that have a registered implementation."""
    return sorted(_REGISTRY.keys())


def get_transition(kind: str) -> Transition:
    """Return a fresh instance of the transition class registered for *kind*.

    Raises :class:`KeyError` when *kind* has no registered implementation.
    """
    if kind == "auto":
        raise ValueError(
            "'auto' must be resolved by the auto-mix planner to a concrete "
            "transition kind before get_transition() / plan_transition()."
        )
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise KeyError(
            "No transition registered for kind={0!r}. Registered kinds: {1}".format(
                kind, registered_kinds()
            )
        )
    return cls()


def plan_transition(
    choice: TransitionChoice, *, fps: float = 24.0
) -> TransitionPlan:
    """Resolve *choice* to a concrete :class:`TransitionPlan`.

    Mirrors :class:`TransitionChoice` 1-to-1 — pass in what the project
    model holds, get back what the applier should run on the clips.
    """
    if choice.duration_frames < 0:
        raise ValueError(
            "TransitionChoice.duration_frames must be >= 0, got {0}".format(
                choice.duration_frames
            )
        )
    impl = get_transition(choice.kind)
    return impl.plan(choice.duration_frames, params=dict(choice.params), fps=fps)


__all__ = [
    "COMPOSITE_MODES",
    "ClipPlan",
    "PointKeyframe",
    "RgbColor",
    "ScalarKeyframe",
    "Transition",
    "TransitionPlan",
    "get_transition",
    "plan_transition",
    "register",
    "registered_kinds",
]
