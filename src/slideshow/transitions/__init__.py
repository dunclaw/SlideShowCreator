"""Transition planning framework.

Pure-Python layer that maps each :class:`~slideshow.project_model.TransitionChoice`
to a :class:`TransitionPlan` describing what the Fusion-comp applier
should do for the outgoing and incoming sides of the transition. This
package never imports Resolve and is fully unit-testable.

Entry points:

* :func:`plan_transition` — high-level dispatcher; takes a
  ``TransitionChoice`` from the project model, returns a
  ``TransitionPlan``.
* :func:`merge_clip_plans` / :func:`apply_comp_spec` — the applier side:
  fold a clip's two neighbouring plans into one ``CompSpec`` and build the
  Fusion node graph for it.
* :func:`get_transition` — lower-level lookup of the
  :class:`Transition` impl for one kind name.
* :func:`registered_kinds` — list every concrete kind that has an
  implementation registered. Should match
  ``project_model.TRANSITION_KINDS`` minus ``{"auto"}``.

Importing this package eagerly imports every implementation module so
each one's :func:`~slideshow.transitions.base.register` decorator runs
and the registry is fully populated.
"""

from __future__ import annotations

from .base import (
    COMPOSITE_MODES,
    NOMINAL_SLIDE_FRAMES,
    ClipPlan,
    PointKeyframe,
    RgbColor,
    ScalarKeyframe,
    Transition,
    TransitionPlan,
    get_transition,
    plan_transition,
    register,
    registered_kinds,
    resolve_duration_frames,
    reverse_clip_plan,
    reverse_keyframes,
    reverse_transform,
    wants_outgoing_on_top,
)

# Eagerly import every implementation module so their @register decorators
# populate the registry. Each import is a no-op if already loaded.
from . import dissolves as _dissolves  # noqa: F401
from . import fades as _fades  # noqa: F401
from . import effects as _effects  # noqa: F401
from . import geometry as _geometry  # noqa: F401
from . import flip as _flip  # noqa: F401
from . import drop as _drop  # noqa: F401
from . import page_turn as _page_turn  # noqa: F401

from .applier import (  # noqa: E402  (must follow the registry imports)
    CompSpec,
    apply_comp_spec,
    apply_composite_mode,
    build_comp_graph,
    comp_spec_for_clip,
    merge_clip_plans,
)


__all__ = [
    "COMPOSITE_MODES",
    "NOMINAL_SLIDE_FRAMES",
    "ClipPlan",
    "CompSpec",
    "PointKeyframe",
    "RgbColor",
    "ScalarKeyframe",
    "Transition",
    "TransitionPlan",
    "apply_comp_spec",
    "apply_composite_mode",
    "build_comp_graph",
    "comp_spec_for_clip",
    "get_transition",
    "merge_clip_plans",
    "plan_transition",
    "register",
    "registered_kinds",
    "resolve_duration_frames",
    "reverse_clip_plan",
    "reverse_keyframes",
    "reverse_transform",
    "wants_outgoing_on_top",
]
