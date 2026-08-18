"""Transition framework abstractions.

A *transition* describes how one slide gives way to the next during an
N-frame overlap. This module is pure data + planning logic — it never
touches Resolve. The plans it emits are consumed later by an applier
(separate module) that pokes Fusion comps via ``slideshow.fusion_comps``.

Layout assumption (encoded in plan semantics):

* :meth:`Transition.plan` always plans for the **incoming clip on the
  upper video track**: it plays over the outgoing clip's tail and almost
  every kind animates the incoming side. A few (push, flip) animate both.
* Slides alternate between V1 and V2, so for every other transition the
  incoming clip lands *underneath* the outgoing one. Animating it there
  would be invisible — the opaque outgoing clip covers it. For those,
  :meth:`Transition.mirror` re-expresses the plan so the clip on the
  upper track (the outgoing one) animates *away* to reveal the incoming
  clip beneath. :func:`plan_transition` picks the right form via its
  ``incoming_on_top`` argument.

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

from ..fusion_comps import (
    PageTurnAnimation,
    PointKeyframe,
    ScalarKeyframe,
    TransformAnimation,
)
from ..project_model import (
    DEFAULT_TRANSITION_DURATION_FRAMES,
    TransitionChoice,
)

#: Slide length assumed when a transition has to be sized with no layout to
#: measure against. Chosen so the default fraction reproduces
#: :data:`~slideshow.project_model.DEFAULT_TRANSITION_DURATION_FRAMES`.
NOMINAL_SLIDE_FRAMES = 80


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
    * ``background_color``— RGB triple. Non-None means insert an opaque
                            Background of this colour *beneath the clip's
                            own image*, so the clip can dip through the
                            colour. Driven by ``color_blend``.
    * ``color_blend``     — opacity of the clip's image over
                            ``background_color``: ``1`` shows the image,
                            ``0`` shows the solid colour. Ignored when
                            ``background_color`` is None.
    * ``background_from_image``
                          — take the dip colour from the clip's own picture
                            (its average, saturation-boosted) instead of the
                            fixed ``background_color``. ``background_color``
                            is still carried as the fallback.
    * ``composite_mode``  — how this clip composites onto the *other*
                            side of the transition. Only meaningful on
                            the clip that ends up on the **upper** track.
    * ``page_turn``       — 3D page rotating about one edge. Non-None
                            replaces the whole 2D image chain with a Fusion
                            3D scene, so it cannot be combined with
                            ``transform`` / ``blur_size`` / ``pixelate_size``.
    """

    transform: Optional[TransformAnimation] = None
    blend: Optional[List[ScalarKeyframe]] = None
    blur_size: Optional[List[ScalarKeyframe]] = None
    pixelate_size: Optional[List[ScalarKeyframe]] = None
    background_color: Optional[RgbColor] = None
    color_blend: Optional[List[ScalarKeyframe]] = None
    background_from_image: bool = False
    composite_mode: str = "normal"
    page_turn: Optional[PageTurnAnimation] = None

    def __post_init__(self) -> None:
        if self.composite_mode not in COMPOSITE_MODES:
            raise ValueError(
                "ClipPlan.composite_mode must be one of {0}, got {1!r}".format(
                    sorted(COMPOSITE_MODES), self.composite_mode
                )
            )
        if self.page_turn is not None and not self.page_turn.is_empty():
            clashes = sorted(
                name
                for name in ("blur_size", "pixelate_size", "transform")
                if getattr(self, name)
            )
            if clashes:
                raise ValueError(
                    "ClipPlan.page_turn replaces the 2D image chain, so it "
                    "cannot be combined with {0}".format(", ".join(clashes))
                )

    def is_empty(self) -> bool:
        """True when this plan has no effects (transform/blend/blur/etc.)."""
        return (
            (self.transform is None or self.transform.is_empty())
            and not self.blend
            and not self.blur_size
            and not self.pixelate_size
            and not self.color_blend
            and self.background_color is None
            and not self.background_from_image
            and self.composite_mode == "normal"
            and (self.page_turn is None or self.page_turn.is_empty())
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
# Mirroring (track-order adaptation)
# --------------------------------------------------------------------------- #

def reverse_keyframes(
    keyframes: Optional[Sequence[Any]], duration_frames: int
) -> Optional[List[Any]]:
    """Play *keyframes* backwards within a ``duration_frames`` window.

    ``(f, v)`` becomes ``(duration_frames - f, v)``, re-sorted. Works for
    both scalar and Point keyframes because only the frame number moves.
    """
    if not keyframes:
        return None
    flipped = [(duration_frames - int(frame), value) for frame, value in keyframes]
    flipped.sort(key=lambda item: item[0])
    return flipped


def reverse_transform(
    animation: Optional[TransformAnimation], duration_frames: int
) -> Optional[TransformAnimation]:
    """Time-reverse every channel of a :class:`TransformAnimation`."""
    if animation is None or animation.is_empty():
        return None
    reversed_anim = TransformAnimation(
        center=reverse_keyframes(animation.center, duration_frames),
        size=reverse_keyframes(animation.size, duration_frames),
        angle=reverse_keyframes(animation.angle, duration_frames),
        pivot=reverse_keyframes(animation.pivot, duration_frames),
    )
    return None if reversed_anim.is_empty() else reversed_anim


def reverse_page_turn(
    animation: Optional[PageTurnAnimation], duration_frames: int
) -> Optional[PageTurnAnimation]:
    """Time-reverse a page turn, so a page arriving instead departs.

    The hinge edge is left alone: a page still pivots on the same edge, it
    just swings the other way in time.
    """
    if animation is None or animation.is_empty():
        return None
    return PageTurnAnimation(
        angle=animation.reversed_angle(duration_frames),
        hinge=animation.hinge,
        focal_length=animation.focal_length,
        cull_backface=animation.cull_backface,
    )


def reverse_clip_plan(plan: Optional[ClipPlan], duration_frames: int) -> ClipPlan:
    """Time-reverse every animated channel of a :class:`ClipPlan`.

    Static attributes (``background_color``, ``background_from_image``,
    ``composite_mode``) ride along unchanged — they describe *what* the clip
    composites against, not when.
    """
    if plan is None:
        return ClipPlan()
    return ClipPlan(
        transform=reverse_transform(plan.transform, duration_frames),
        blend=reverse_keyframes(plan.blend, duration_frames),
        blur_size=reverse_keyframes(plan.blur_size, duration_frames),
        pixelate_size=reverse_keyframes(plan.pixelate_size, duration_frames),
        background_color=plan.background_color,
        color_blend=reverse_keyframes(plan.color_blend, duration_frames),
        background_from_image=plan.background_from_image,
        composite_mode=plan.composite_mode,
        page_turn=reverse_page_turn(plan.page_turn, duration_frames),
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

    #: Does this transition animate the *outgoing* slide rather than the
    #: incoming one? Almost all animate the incoming slide arriving over the
    #: settled one, and the layout puts that slide on the upper track. A few
    #: only read the other way round — a page peeling away to reveal the next
    #: photo underneath is the outgoing slide moving, and it has to be on top
    #: to be seen at all. Setting this makes the layout lift the outgoing
    #: slide instead, which in turn makes :func:`plan_transition` mirror the
    #: plan so the animation lands on the clip that is actually visible.
    PREFERS_OUTGOING_ON_TOP: ClassVar[bool] = False

    #: How long this transition wants to be, as a fraction of the shorter of
    #: the two slides it joins, when the project doesn't say. Fixed frame
    #: counts don't survive a change of slide length: 30 frames is a quarter
    #: of a 4-second slide and a twelfth of a 12-second one, so the same
    #: transition goes from measured to perfunctory without anything about it
    #: changing. Every plan is already written in fractions of
    #: ``duration_frames``, so scaling the number is all that's needed.
    DURATION_FRACTION: ClassVar[float] = 0.30

    #: Floor and ceiling on the derived length. The floor stops a very short
    #: slide producing a transition too brief to perceive; the ceiling stops a
    #: long slide turning a dissolve into the main event. Kinds that need
    #: room to read — anything with a hold, a bounce or an arc — raise the
    #: ceiling rather than the fraction, so they grow with the slide but only
    #: up to the point where they stop being a transition.
    MIN_DURATION_FRAMES: ClassVar[int] = 8
    MAX_DURATION_FRAMES: ClassVar[int] = 48

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

    def mirror(self, plan: TransitionPlan) -> TransitionPlan:
        """Re-express *plan* for the case where the incoming clip is below.

        The default is a straight time-reversal with the two sides swapped:
        whatever the incoming clip did to reveal itself, the outgoing clip
        now does backwards to hide itself. That is correct for every kind
        driven by opacity (dissolves, fades, zooms, pixelate, flip) and is
        the right fallback for anything else.

        Kinds whose look depends on a named direction override this — see
        :mod:`slideshow.transitions.geometry`.
        """
        duration = plan.duration_frames
        if duration <= 0:
            return plan
        return TransitionPlan(
            kind=plan.kind,
            duration_frames=duration,
            incoming=reverse_clip_plan(plan.outgoing, duration),
            outgoing=reverse_clip_plan(plan.incoming, duration),
        )


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


def wants_outgoing_on_top(choice: Optional[TransitionChoice]) -> bool:
    """Does *choice* need the outgoing slide on the upper video track?

    The layout asks this per boundary to decide whether to carve a HEAD off
    the incoming slide or a TAIL off the outgoing one. Unknown kinds, cuts
    and unresolved ``auto`` all answer "no", so an unexpected value degrades
    to the ordinary incoming-on-top layout rather than failing the build.
    """
    if choice is None or choice.is_cut():
        return False
    cls = _REGISTRY.get(choice.kind)
    return bool(cls is not None and cls.PREFERS_OUTGOING_ON_TOP)


def resolve_duration_frames(
    choice: Optional[TransitionChoice],
    slide_frames: Optional[int] = None,
) -> int:
    """How many frames *choice* should overlap, resolving ``None``.

    An explicit ``duration_frames`` is returned untouched — the project
    always wins. ``None`` means "derive it", and the length then comes from
    the transition's own :attr:`~Transition.DURATION_FRACTION` applied to
    *slide_frames*, clamped between its
    :attr:`~Transition.MIN_DURATION_FRAMES` and
    :attr:`~Transition.MAX_DURATION_FRAMES`.

    *slide_frames* should be the shorter of the two slides the transition
    joins, since that is the one that constrains it. Passing ``None`` falls
    back to :data:`~slideshow.project_model.DEFAULT_TRANSITION_DURATION_FRAMES`
    scaled as if for a nominal slide, which is only for callers planning a
    transition outside a layout.

    Unknown kinds and cuts resolve to 0, matching
    :func:`wants_outgoing_on_top` in degrading rather than raising.
    """
    if choice is None or choice.kind == "none":
        return 0
    if choice.duration_frames is not None:
        return max(0, int(choice.duration_frames))

    cls = _REGISTRY.get(choice.kind)
    if cls is None:
        return DEFAULT_TRANSITION_DURATION_FRAMES
    if slide_frames is None:
        # No layout context. Derive against a nominal slide so the per-kind
        # ratios still hold relative to each other.
        slide_frames = NOMINAL_SLIDE_FRAMES
    if slide_frames <= 0:
        return 0

    derived = int(round(cls.DURATION_FRACTION * slide_frames))
    low = max(0, int(cls.MIN_DURATION_FRAMES))
    high = max(low, int(cls.MAX_DURATION_FRAMES))
    return max(low, min(high, derived))


def plan_transition(
    choice: TransitionChoice,
    *,
    fps: float = 24.0,
    incoming_on_top: bool = True,
    slide_frames: Optional[int] = None,
) -> TransitionPlan:
    """Resolve *choice* to a concrete :class:`TransitionPlan`.

    Mirrors :class:`TransitionChoice` 1-to-1 — pass in what the project
    model holds, get back what the applier should run on the clips.

    ``incoming_on_top`` says whether the incoming clip sits on the higher
    video track. When it doesn't, the plan is mirrored (see
    :meth:`Transition.mirror`) so the animation lands on the clip that is
    actually visible.

    ``slide_frames`` sizes a ``duration_frames=None`` choice — see
    :func:`resolve_duration_frames`. The builder normally hands over a
    choice the layout has already resolved, so this rarely matters.
    """
    if choice.duration_frames is not None and choice.duration_frames < 0:
        raise ValueError(
            "TransitionChoice.duration_frames must be >= 0 or None, got {0}".format(
                choice.duration_frames
            )
        )
    duration = resolve_duration_frames(choice, slide_frames)
    impl = get_transition(choice.kind)
    plan = impl.plan(duration, params=dict(choice.params), fps=fps)
    if not incoming_on_top:
        plan = impl.mirror(plan)
    return plan


__all__ = [
    "COMPOSITE_MODES",
    "ClipPlan",
    "PageTurnAnimation",
    "PointKeyframe",
    "RgbColor",
    "ScalarKeyframe",
    "Transition",
    "TransitionPlan",
    "NOMINAL_SLIDE_FRAMES",
    "get_transition",
    "plan_transition",
    "register",
    "registered_kinds",
    "resolve_duration_frames",
    "reverse_clip_plan",
    "reverse_keyframes",
    "reverse_page_turn",
    "reverse_transform",
    "wants_outgoing_on_top",
]
