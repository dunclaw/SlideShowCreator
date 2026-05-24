"""Helpers for building Fusion compositions on top of a Resolve timeline item.

Resolve's ``TimelineItem.SetProperty`` only sets constants — it cannot keyframe
Pan/Tilt/Zoom/Rotation/Opacity over time. To animate those (which we need for
real transitions: slide, push, zoom, flip, drop, dissolve), every transition
attaches a Fusion composition to a timeline item and constructs a small node
graph: MediaIn1 → Transform → MediaOut1 (plus optional Merge/Background/Blur).

This module deliberately stays:

* **Stdlib-only** so it runs under Resolve's bundled Python 3.6 interpreter.
* **Thin** — one verb per function — so transition modules can compose without
  re-discovering Fusion API conventions.
* **Mockable** — every public function takes the Resolve/Fusion object as an
  argument; nothing reaches out to the global ``ResolveContext`` itself.

Fusion API notes (the bits we rely on):

* ``timeline_item.AddFusionComp()`` adds a comp and returns it. Resolve
  auto-wires a ``MediaIn1`` → ``MediaOut1`` graph inside.
* ``comp.Lock() / comp.Unlock()`` brackets atomic edits.
* ``comp.FindTool(name)`` returns a tool by its name (``"MediaIn1"`` etc.).
* ``comp.AddTool(toolType, x, y)`` adds a tool at the given flow position.
* ``dst_tool.ConnectInput(input_name, src_tool.Output)`` wires outputs to
  inputs. ``dst_tool.Input = src_tool.Output`` is a shorthand for the default
  ``"Input"`` parameter.
* ``tool.SetInput(name, value, time)`` sets a parameter; supplying multiple
  time values creates an animated spline behind the scenes. For ``Point``
  inputs (e.g. ``Center``) the value is a ``(x, y)`` tuple or list.

If the live behavior of ``SetInput`` keyframing turns out to differ in any
Resolve build, the breakage is isolated to ``_apply_keyframes`` and the rest
of the abstraction stays intact.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union


# --------------------------------------------------------------------------- #
# Type aliases
# --------------------------------------------------------------------------- #

ScalarKeyframe = Tuple[Union[int, float], float]
PointKeyframe = Tuple[Union[int, float], Tuple[float, float]]


DEFAULT_COMP_NAME = "SlideShowCreator"
DEFAULT_TRANSFORM_NAME = "SlideShowXf"


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def locked(comp: Any):
    """Context manager that brackets edits in ``comp.Lock()`` / ``Unlock()``.

    Fusion strongly prefers batched edits to land inside a Lock/Unlock pair —
    otherwise every micro-change triggers a UI refresh and may produce a
    visible flicker in the Fusion page.
    """
    comp.Lock()
    try:
        yield comp
    finally:
        comp.Unlock()


# --------------------------------------------------------------------------- #
# Comp lifecycle on a timeline item
# --------------------------------------------------------------------------- #

def attach_or_get_comp(timeline_item: Any, name: str = DEFAULT_COMP_NAME) -> Any:
    """Return the comp named *name* on *timeline_item*, creating it if missing.

    Resolve happily lets a single timeline item carry multiple comps — we want
    exactly one named comp for SlideShowCreator's use, so the function is
    idempotent: calling it again returns the same comp rather than stacking
    duplicates.
    """
    existing_names = timeline_item.GetFusionCompNameList() or []
    if name in existing_names:
        return timeline_item.GetFusionCompByName(name)

    comp = timeline_item.AddFusionComp()
    if comp is None:
        raise RuntimeError(
            "TimelineItem.AddFusionComp() returned None for {0!r}.".format(
                timeline_item.GetName() if hasattr(timeline_item, "GetName") else "?"
            )
        )

    # Newly-added comps get an auto-generated name ("Composition 1", etc.).
    # Rename so subsequent calls find it.
    try:
        current = comp.GetAttrs("COMPS_Name") if hasattr(comp, "GetAttrs") else None
    except Exception:
        current = None
    if current and current != name:
        timeline_item.RenameFusionCompByName(current, name)
    return comp


def remove_comp(timeline_item: Any, name: str = DEFAULT_COMP_NAME) -> bool:
    """Delete the named comp from *timeline_item* if present."""
    existing_names = timeline_item.GetFusionCompNameList() or []
    if name not in existing_names:
        return False
    return bool(timeline_item.DeleteFusionCompByName(name))


# --------------------------------------------------------------------------- #
# Tool lookup and wiring
# --------------------------------------------------------------------------- #

def find_tool(comp: Any, name: str) -> Optional[Any]:
    """Return the tool named *name* in *comp*, or ``None``."""
    return comp.FindTool(name)


def find_or_add_tool(
    comp: Any,
    tool_type: str,
    name: str,
    *,
    position: Tuple[int, int] = (0, 0),
) -> Any:
    """Return the tool named *name*, creating it as *tool_type* if missing.

    The Fusion flow-view position (used only to keep node graphs readable when
    a user opens the comp) is honored when creating; existing tools are not
    moved.
    """
    existing = comp.FindTool(name)
    if existing is not None:
        return existing
    x, y = position
    tool = comp.AddTool(tool_type, x, y)
    if tool is None:
        raise RuntimeError(
            "comp.AddTool({0!r}) returned None".format(tool_type)
        )
    try:
        tool.SetAttrs({"TOOLS_Name": name})
    except Exception:
        # Older Fusion builds reject some attr keys; the tool still works.
        pass
    return tool


def connect(
    src_tool: Any,
    dst_tool: Any,
    dst_input: str = "Input",
) -> None:
    """Wire ``src_tool.Output`` into ``dst_tool[dst_input]``.

    Uses ``ConnectInput(name, output)`` which works regardless of whether the
    Python binding exposes inputs as attributes.
    """
    out = getattr(src_tool, "Output", None)
    if out is None:
        out = src_tool.GetOutput("Output") if hasattr(src_tool, "GetOutput") else None
    if out is None:
        raise RuntimeError(
            "Source tool has no Output to connect from: {0!r}".format(src_tool)
        )
    dst_tool.ConnectInput(dst_input, out)


# --------------------------------------------------------------------------- #
# Keyframe application
# --------------------------------------------------------------------------- #

def _apply_keyframes(
    tool: Any,
    input_name: str,
    keyframes: Sequence[Tuple[Union[int, float], Any]],
) -> None:
    """Apply a sorted list of ``(time, value)`` keyframes to a tool input.

    Calling ``SetInput(name, value, time)`` repeatedly is the Fusion pattern
    that produces an animated input. With a single keyframe the input becomes
    a constant at that value; with two or more, Fusion auto-creates a spline.

    Values for vector inputs (Point) should be a ``(x, y)`` tuple or list.
    Scalar values are plain ``float`` / ``int``.
    """
    if not keyframes:
        return
    ordered = sorted(keyframes, key=lambda kv: kv[0])
    for time, value in ordered:
        tool.SetInput(input_name, value, time)


def set_scalar_keyframes(
    tool: Any,
    input_name: str,
    keyframes: Sequence[ScalarKeyframe],
) -> None:
    """Animate a scalar input (Size, Angle, Gain, Blend, …)."""
    _apply_keyframes(tool, input_name, list(keyframes))


def set_point_keyframes(
    tool: Any,
    input_name: str,
    keyframes: Sequence[PointKeyframe],
) -> None:
    """Animate a Point input (Center, Pivot, …). Values are ``(x, y)`` tuples.

    Fusion's coordinate space for Point inputs is normalized: ``(0.5, 0.5)``
    is the frame center; ``(0, 0.5)`` is the left edge.
    """
    _apply_keyframes(tool, input_name, list(keyframes))


def set_constant(tool: Any, input_name: str, value: Any) -> None:
    """Set a single non-animated value on an input."""
    tool.SetInput(input_name, value)


# --------------------------------------------------------------------------- #
# Common comp patterns
# --------------------------------------------------------------------------- #

def insert_transform_chain(
    comp: Any,
    *,
    transform_name: str = DEFAULT_TRANSFORM_NAME,
    media_in_name: str = "MediaIn1",
    media_out_name: str = "MediaOut1",
    position: Tuple[int, int] = (1, 0),
) -> Any:
    """Insert a Transform tool between MediaIn1 and MediaOut1, return it.

    Idempotent: if a Transform named *transform_name* is already chained in,
    returns it without re-inserting. The graph after the call is always::

        MediaIn1.Output ─> Transform.Input
        Transform.Output ─> MediaOut1.Input
    """
    media_in = comp.FindTool(media_in_name)
    media_out = comp.FindTool(media_out_name)
    if media_in is None:
        raise RuntimeError(
            "Composition has no {0!r} tool — is this a Resolve clip comp?".format(
                media_in_name
            )
        )
    if media_out is None:
        raise RuntimeError(
            "Composition has no {0!r} tool — is this a Resolve clip comp?".format(
                media_out_name
            )
        )

    xform = find_or_add_tool(comp, "Transform", transform_name, position=position)

    connect(media_in, xform, "Input")
    connect(xform, media_out, "Input")
    return xform


# --------------------------------------------------------------------------- #
# Declarative transform animation
# --------------------------------------------------------------------------- #

class TransformAnimation:
    """A declarative description of a Transform tool's animation.

    Each field is an optional list of keyframes:

    * ``center``     — list of ``(frame, (x, y))``     in normalized coords
    * ``size``       — list of ``(frame, scalar)``     1.0 = original size
    * ``angle``      — list of ``(frame, degrees)``    counter-clockwise
    * ``pivot``      — list of ``(frame, (x, y))``     pivot for rotation/scale

    A frame value is relative to the **start of the Fusion comp**, which
    matches the start of the timeline clip. Frame 0 is the first visible
    frame of the clip.

    Lists with a single keyframe set a constant. Empty / ``None`` fields
    leave the input alone (Fusion defaults: Center=(0.5,0.5), Size=1.0,
    Angle=0.0, Pivot=(0.5,0.5)).
    """

    __slots__ = ("center", "size", "angle", "pivot")

    def __init__(
        self,
        *,
        center: Optional[Sequence[PointKeyframe]] = None,
        size: Optional[Sequence[ScalarKeyframe]] = None,
        angle: Optional[Sequence[ScalarKeyframe]] = None,
        pivot: Optional[Sequence[PointKeyframe]] = None,
    ) -> None:
        self.center = list(center) if center else None
        self.size = list(size) if size else None
        self.angle = list(angle) if angle else None
        self.pivot = list(pivot) if pivot else None

    def is_empty(self) -> bool:
        return not (self.center or self.size or self.angle or self.pivot)


def apply_transform_animation(
    transform_tool: Any, animation: TransformAnimation
) -> None:
    """Push a :class:`TransformAnimation` onto a Transform tool."""
    if animation.center:
        set_point_keyframes(transform_tool, "Center", animation.center)
    if animation.pivot:
        set_point_keyframes(transform_tool, "Pivot", animation.pivot)
    if animation.size:
        set_scalar_keyframes(transform_tool, "Size", animation.size)
    if animation.angle:
        set_scalar_keyframes(transform_tool, "Angle", animation.angle)


def attach_transform_animation(
    timeline_item: Any,
    animation: TransformAnimation,
    *,
    comp_name: str = DEFAULT_COMP_NAME,
    transform_name: str = DEFAULT_TRANSFORM_NAME,
) -> Any:
    """End-to-end helper: ensure a comp + Transform exist and apply *animation*.

    Returns the Transform tool. All Fusion edits happen inside a single
    Lock/Unlock pair.
    """
    comp = attach_or_get_comp(timeline_item, comp_name)
    with locked(comp):
        xform = insert_transform_chain(comp, transform_name=transform_name)
        apply_transform_animation(xform, animation)
    return xform


# --------------------------------------------------------------------------- #
# Tool inventory (mostly for diagnostics / live tests)
# --------------------------------------------------------------------------- #

def list_tools(comp: Any) -> List[str]:
    """Return the names of all tools in a comp (for diagnostics)."""
    tools = comp.GetToolList(False) or {}
    # GetToolList returns a 1-indexed dict — we just want names.
    names: List[str] = []
    if isinstance(tools, dict):
        for key in sorted(tools.keys()):
            t = tools[key]
            try:
                names.append(t.Name)
            except Exception:
                names.append(str(key))
    return names


__all__ = [
    "DEFAULT_COMP_NAME",
    "DEFAULT_TRANSFORM_NAME",
    "PointKeyframe",
    "ScalarKeyframe",
    "TransformAnimation",
    "apply_transform_animation",
    "attach_or_get_comp",
    "attach_transform_animation",
    "connect",
    "find_or_add_tool",
    "find_tool",
    "insert_transform_chain",
    "list_tools",
    "locked",
    "remove_comp",
    "set_constant",
    "set_point_keyframes",
    "set_scalar_keyframes",
]
