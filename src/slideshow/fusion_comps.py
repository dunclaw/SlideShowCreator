"""Helpers for building Fusion compositions on top of a Resolve timeline item.

Resolve's ``TimelineItem.SetProperty`` only sets constants — it cannot keyframe
Pan/Tilt/Zoom/Rotation/Opacity over time. To animate those (which we need for
real transitions: slide, push, zoom, flip, drop, dissolve), every transition
attaches a Fusion composition to a timeline item and constructs a small node
graph: MediaIn1 → Transform → MediaOut1 (plus optional Merge/Background/Blur).

The Fusion scripting API in DaVinci Resolve 20.3 has a number of sharp edges
this module hides:

* ``clip.AddFusionComp()`` returns a *stub* handle whose edits silently do not
  persist. Don't use it. Always go through
  ``clip.LoadFusionCompByName(name)`` and edit through *that* return value.
* ``fusion.GetCurrentComp()`` follows the **UI selection**, not the comp you
  just loaded — using it for scripted edits routes them to the wrong clip.
* ``clip.RenameFusionCompByName(...)`` returns ``False`` and is a no-op, so
  we never rely on giving a comp a custom name. We always edit the FIRST
  comp on the clip (the one Resolve actually renders on the timeline).
* ``tool.SetInput(name, value, time)`` does NOT keyframe — the ``time``
  argument is ignored and only the last value survives as a constant. To
  animate a scalar input, build a ``BezierSpline`` modifier and connect it.
  To animate a Point input (Center, Pivot, …), use
  ``tool.AddModifier(input, "XYPath")`` then drive the XYPath's X and Y
  child inputs with their own BezierSpline children. Plain
  ``comp.AddTool("XYPath") + ConnectInput`` does NOT work for Point inputs
  because the XYPath's ``.Output`` is a Path type, not a Point — Fusion
  silently drops the connection.
* Even after correct edits, Resolve will not re-render the clip on the
  Edit page until ``comp.SetAttrs({"COMPB_Modified": True})`` is called.
  That flag is what produces the "3 yellow dots" modified marker.

If any of these behaviors change in a future Resolve build, the breakage will
be isolated to the helpers in this file.
"""

from __future__ import annotations

import contextlib
from typing import Any, List, Optional, Sequence, Tuple, Union


# --------------------------------------------------------------------------- #
# Type aliases
# --------------------------------------------------------------------------- #

ScalarKeyframe = Tuple[Union[int, float], float]
PointKeyframe = Tuple[Union[int, float], Tuple[float, float]]


DEFAULT_TRANSFORM_NAME = "SlideShowXf"
DEFAULT_BLUR_NAME = "SlideShowBlur"
DEFAULT_PIXELATE_NAME = "SlideShowPixelate"
DEFAULT_BACKGROUND_NAME = "SlideShowBg"
DEFAULT_MERGE_NAME = "SlideShowMerge"
#: Inner "dip" pair: a solid colour the clip's image is blended against
#: *before* the outer merge controls its overall opacity. Only built for
#: dip-to-colour style transitions.
DEFAULT_COLOR_BACKGROUND_NAME = "SlideShowDipBg"
DEFAULT_COLOR_MERGE_NAME = "SlideShowDipMerge"

#: Fusion tool IDs. ``Blur`` is a native Fusion tool, but there is **no**
#: native pixelate tool — ``comp.AddTool("Pixelate")`` returns ``None``. The
#: effect is only available as a ResolveFX OFX plugin, addressed by its full
#: reverse-DNS ID. Verified against Resolve Studio 20.3.3.
BLUR_TOOL = "Blur"
PIXELATE_TOOL = "ofx.com.blackmagicdesign.resolvefx.MosaicBlur"

#: Fusion input names for the scalar "amount" of each effect tool.
#: ``Blur`` locks X to Y by default, so driving ``XBlurSize`` animates both
#: axes. The OFX mosaic tool exposes a single ``PixelFrequency`` control.
BLUR_SIZE_INPUT = "XBlurSize"
PIXELATE_SIZE_INPUT = "PixelFrequency"

#: Width, in pixels, that :func:`pixel_size_to_frequency` assumes when turning
#: a block edge length into a cell count.
PIXELATE_REFERENCE_WIDTH = 1920.0

#: Clamp for the converted frequency. Below 2 the whole frame collapses to a
#: single flat cell; above this the mosaic is finer than one pixel.
PIXELATE_MIN_FREQUENCY = 2.0
PIXELATE_MAX_FREQUENCY = 2000.0


def pixel_size_to_frequency(
    size: float, reference_width: float = PIXELATE_REFERENCE_WIDTH
) -> float:
    """Convert a mosaic block edge length into ResolveFX ``PixelFrequency``.

    Transition plans describe pixelation the intuitive way — as a block size,
    where ``1.0`` means "untouched" and larger means chunkier. ``PixelFrequency``
    is the **reciprocal**: it counts cells across the frame, so *larger* means
    *finer*. Writing a block size straight into it inverts the effect, and a
    frequency of 1 turns the entire frame into a single flat colour.
    """
    if size <= 0:
        return PIXELATE_MAX_FREQUENCY
    freq = reference_width / float(size)
    return max(PIXELATE_MIN_FREQUENCY, min(PIXELATE_MAX_FREQUENCY, freq))


#: The image input of a tool is ``Input`` on native Fusion tools but
#: ``Source`` on ResolveFX OFX plugins — connecting to the wrong one
#: silently leaves the tool unwired.
TOOL_IMAGE_INPUTS = {
    PIXELATE_TOOL: "Source",
}


def primary_image_input(tool_type: str) -> str:
    """Name of the main image input for *tool_type* (``Input`` for most tools)."""
    return TOOL_IMAGE_INPUTS.get(tool_type, "Input")


#: ``Merge.ApplyMode`` values matching ``ClipPlan.composite_mode``.
MERGE_APPLY_MODES = {
    "normal": "Normal",
    "add": "Add",
    "non_add": "Maximum",
}


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def locked(comp: Any):
    """Bracket a batch of edits in ``comp.Lock()`` / ``Unlock()``.

    Fusion strongly prefers batched edits to land inside a Lock/Unlock pair —
    otherwise every micro-change triggers a UI refresh.
    """
    comp.Lock()
    try:
        yield comp
    finally:
        comp.Unlock()


# --------------------------------------------------------------------------- #
# Comp lifecycle on a timeline item
# --------------------------------------------------------------------------- #

def get_active_comp(timeline_item: Any) -> Any:
    """Return a handle to the *active* Fusion comp on *timeline_item*.

    "Active" = the first comp on the clip; that is the one Resolve renders
    on the Edit-page timeline. If the clip has no comp yet (a fresh clip
    that was never opened on the Fusion page), one is created here so we
    have something to edit.

    The returned handle is obtained via ``LoadFusionCompByName`` — that is
    the *only* call whose return value gives a comp object that scripted
    edits actually persist through. ``GetFusionCompByName`` and the value
    returned by ``AddFusionComp`` look superficially the same but do NOT
    let edits land in the comp Resolve renders.
    """
    names = timeline_item.GetFusionCompNameList() or []
    if not names:
        added_name = timeline_item.AddFusionComp()
        if not added_name:
            raise RuntimeError(
                "TimelineItem.AddFusionComp() did not create a comp on {0!r}.".format(
                    _safe_name(timeline_item)
                )
            )
        names = timeline_item.GetFusionCompNameList() or []
        if not names:
            raise RuntimeError(
                "TimelineItem reports no comps after AddFusionComp() on {0!r}.".format(
                    _safe_name(timeline_item)
                )
            )
    comp = timeline_item.LoadFusionCompByName(names[0])
    if comp is None:
        raise RuntimeError(
            "LoadFusionCompByName({0!r}) returned None on {1!r}.".format(
                names[0], _safe_name(timeline_item)
            )
        )
    return comp


# Backwards-compatible alias used by older callers / tests.
attach_or_get_comp = get_active_comp


def mark_modified(comp: Any) -> None:
    """Tell Resolve that *comp* has changed, so it re-renders the clip.

    Without this call, our scripted edits land in the comp data fine but
    Resolve's Edit-page renderer keeps showing the unmodified frame and
    the clip never gets the "3 yellow dots" modified marker.

    Should be called once after a batch of edits (outside the Lock/Unlock).
    """
    try:
        comp.SetAttrs({"COMPB_Modified": True})
    except Exception:
        # Be tolerant: if a future Resolve build removes this attr we'd
        # rather lose the auto-rerender than crash all consumers.
        pass


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
    """Return the tool named *name*, creating it as *tool_type* if missing."""
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
        pass
    return tool


def connect(src_tool: Any, dst_tool: Any, dst_input: str = "Input") -> None:
    """Wire ``src_tool.Output`` into ``dst_tool[dst_input]``."""
    out = getattr(src_tool, "Output", None)
    if out is None:
        out = src_tool.GetOutput("Output") if hasattr(src_tool, "GetOutput") else None
    if out is None:
        raise RuntimeError(
            "Source tool has no Output to connect from: {0!r}".format(src_tool)
        )
    dst_tool.ConnectInput(dst_input, out)


# --------------------------------------------------------------------------- #
# Keyframe application via BezierSpline modifiers
# --------------------------------------------------------------------------- #

def _tool_attr_name(tool: Any) -> str:
    """Return the tool's name attribute, or ''.

    Used so we can detect a previously-created modifier from a prior run and
    reuse it rather than orphaning splines in the node graph.
    """
    try:
        return tool.GetAttrs("TOOLS_Name") or ""
    except Exception:
        return ""


def set_scalar_keyframes(
    comp: Any,
    tool: Any,
    input_name: str,
    keyframes: Sequence[ScalarKeyframe],
) -> Optional[Any]:
    """Animate a scalar input (Size, Angle, Gain, Blend, …).

    With zero keyframes: no-op.
    With one keyframe:   set as a non-animated constant.
    With two or more:    create a ``BezierSpline`` modifier, populate its
                         keyframes, and connect it to ``tool[input_name]``.
                         Fusion automatically renames the spline to
                         ``<tool><input>`` (e.g. ``SlideShowXfAngle``)
                         when ConnectInput is called.

    Returns the BezierSpline tool created (or ``None`` if no spline was
    needed).
    """
    kfs = sorted(keyframes, key=lambda kv: kv[0]) if keyframes else []
    if not kfs:
        return None
    if len(kfs) == 1:
        tool.SetInput(input_name, float(kfs[0][1]))
        return None

    # Try to reuse an existing connected modifier of the expected name so
    # repeated calls don't pile up orphan splines in the comp.
    expected_name = "{0}{1}".format(_tool_attr_name(tool), input_name)
    spline = comp.FindTool(expected_name) if expected_name else None
    if spline is None:
        spline = comp.AddTool("BezierSpline")
        if spline is None:
            raise RuntimeError(
                "comp.AddTool('BezierSpline') returned None for input {0!r}".format(
                    input_name
                )
            )
    spline.SetKeyFrames({int(t): [float(v)] for t, v in kfs})
    tool.ConnectInput(input_name, spline)
    return spline


def _connected_tool(tool: Any, input_name: str) -> Optional[Any]:
    """Return the tool currently driving ``tool.<input_name>``, or ``None``.

    Used both to detect an already-wired modifier (so we don't add duplicates
    on a re-run) and to navigate from an input to the tool that the
    ``AddModifier`` call just attached.
    """
    inp = getattr(tool, input_name, None)
    if inp is None:
        return None
    gco = getattr(inp, "GetConnectedOutput", None)
    if gco is None:
        return None
    try:
        out = gco()
    except Exception:
        return None
    if out is None:
        return None
    gt = getattr(out, "GetTool", None)
    if gt is None:
        return None
    try:
        return gt()
    except Exception:
        return None


def set_point_keyframes(
    comp: Any,
    tool: Any,
    input_name: str,
    keyframes: Sequence[PointKeyframe],
) -> Optional[Any]:
    """Animate a Point input (Center, Pivot, …) using an XYPath modifier.

    Fusion's Point inputs cannot be animated by a single BezierSpline — they
    require an ``XYPath`` modifier whose own ``X`` and ``Y`` inputs are each
    driven by a BezierSpline child. The canonical wiring is::

        tool.AddModifier(input_name, "XYPath")
        xypath = tool.<input>.GetConnectedOutput().GetTool()
        xypath.AddModifier("X", "BezierSpline")
        xypath.AddModifier("Y", "BezierSpline")
        x_spline = xypath.X.GetConnectedOutput().GetTool()
        y_spline = xypath.Y.GetConnectedOutput().GetTool()
        x_spline.SetKeyFrames({frame: [x_value]})
        y_spline.SetKeyFrames({frame: [y_value]})

    The crucial difference vs. naive ``comp.AddTool("XYPath")`` +
    ``ConnectInput`` is that ``AddModifier`` creates AND binds the modifier
    into the Point input in one atomic step. Plain ``ConnectInput`` does not
    work because the XYPath's ``.Output`` is a ``Path`` type, not a
    ``Point`` — Fusion silently drops the connection.

    With zero keyframes: no-op.
    With one keyframe:   set as a non-animated constant ``(x, y)``.
    With two or more:    build the XYPath + child splines and populate.

    Returns the XYPath tool (or ``None`` if no animation was needed).
    """
    kfs = sorted(keyframes, key=lambda kv: kv[0]) if keyframes else []
    if not kfs:
        return None
    if len(kfs) == 1:
        x, y = kfs[0][1]
        tool.SetInput(input_name, [float(x), float(y)])
        return None

    # Reuse an existing XYPath if a previous run already wired one.
    xypath = _connected_tool(tool, input_name)
    if xypath is None:
        added = tool.AddModifier(input_name, "XYPath")
        if added is False:
            raise RuntimeError(
                "tool.AddModifier({0!r}, 'XYPath') returned False".format(input_name)
            )
        xypath = _connected_tool(tool, input_name)
        if xypath is None:
            raise RuntimeError(
                "AddModifier({0!r}, 'XYPath') did not wire an XYPath".format(input_name)
            )

    # Same dance for X and Y child splines.
    x_spline = _connected_tool(xypath, "X")
    if x_spline is None:
        xypath.AddModifier("X", "BezierSpline")
        x_spline = _connected_tool(xypath, "X")
    y_spline = _connected_tool(xypath, "Y")
    if y_spline is None:
        xypath.AddModifier("Y", "BezierSpline")
        y_spline = _connected_tool(xypath, "Y")
    if x_spline is None or y_spline is None:
        raise RuntimeError(
            "Could not access XYPath X/Y child splines for input {0!r}".format(
                input_name
            )
        )

    x_spline.SetKeyFrames({int(t): [float(v[0])] for t, v in kfs})
    y_spline.SetKeyFrames({int(t): [float(v[1])] for t, v in kfs})
    return xypath


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
    """Insert a Transform tool between MediaIn1 and MediaOut1 and return it.

    Idempotent: re-using a Transform named *transform_name* is fine, and the
    wiring is always (re)established. The graph after the call is always::

        MediaIn1.Output ─> Transform.Input
        Transform.Output ─> MediaOut1.Input
    """
    return insert_tool_chain(
        comp,
        [("Transform", transform_name)],
        media_in_name=media_in_name,
        media_out_name=media_out_name,
        first_position=position,
    )[0]


def _require_tool(comp: Any, name: str) -> Any:
    tool = comp.FindTool(name)
    if tool is None:
        raise RuntimeError(
            "Composition has no {0!r} tool — is this a Resolve clip comp?".format(name)
        )
    return tool


def insert_tool_chain(
    comp: Any,
    specs: Sequence[Tuple[str, str]],
    *,
    media_in_name: str = "MediaIn1",
    media_out_name: str = "MediaOut1",
    first_position: Tuple[int, int] = (1, 0),
) -> List[Any]:
    """Wire an ordered list of tools between MediaIn1 and MediaOut1.

    *specs* is a sequence of ``(tool_type, tool_name)`` pairs, upstream
    first. The resulting graph is always::

        MediaIn1 ─> specs[0] ─> specs[1] ─> … ─> MediaOut1

    Existing tools with the same names are reused (and re-wired), so calling
    this twice never duplicates nodes — important because the builder may be
    re-run over a timeline whose clips already carry our comps.

    Passing an empty *specs* wires MediaIn1 straight to MediaOut1.

    Returns the tools in the same order as *specs*.
    """
    media_in = _require_tool(comp, media_in_name)
    media_out = _require_tool(comp, media_out_name)

    x, y = first_position
    tools: List[Any] = []
    upstream = media_in
    for offset, (tool_type, tool_name) in enumerate(specs):
        tool = find_or_add_tool(
            comp, tool_type, tool_name, position=(x + offset, y)
        )
        connect(upstream, tool, primary_image_input(tool_type))
        tools.append(tool)
        upstream = tool

    connect(upstream, media_out, "Input")
    return tools


def add_blur(
    comp: Any,
    *,
    name: str = DEFAULT_BLUR_NAME,
    position: Tuple[int, int] = (0, 0),
) -> Any:
    """Find or create a Blur tool (unwired — use :func:`insert_tool_chain`)."""
    return find_or_add_tool(comp, BLUR_TOOL, name, position=position)


def add_pixelate(
    comp: Any,
    *,
    name: str = DEFAULT_PIXELATE_NAME,
    position: Tuple[int, int] = (0, 0),
) -> Any:
    """Find or create a pixelate tool (unwired).

    Uses the ResolveFX Mosaic Blur OFX plugin — Fusion has no native
    ``Pixelate`` tool. See :data:`PIXELATE_TOOL`.
    """
    return find_or_add_tool(comp, PIXELATE_TOOL, name, position=position)


def add_background(
    comp: Any,
    color: Tuple[float, float, float],
    *,
    name: str = DEFAULT_BACKGROUND_NAME,
    alpha: float = 1.0,
    position: Tuple[int, int] = (0, 1),
) -> Any:
    """Find or create a solid-colour Background tool set to *color*.

    Fusion's Background tool exposes its colour as four separate scalar
    inputs (``TopLeftRed`` … ``TopLeftAlpha``) rather than one Point/RGBA
    input, so each channel is set individually.
    """
    r, g, b = color
    tool = find_or_add_tool(comp, "Background", name, position=position)
    tool.SetInput("TopLeftRed", float(r))
    tool.SetInput("TopLeftGreen", float(g))
    tool.SetInput("TopLeftBlue", float(b))
    tool.SetInput("TopLeftAlpha", float(alpha))
    return tool


def add_merge(
    comp: Any,
    *,
    background: Any,
    foreground: Any,
    name: str = DEFAULT_MERGE_NAME,
    apply_mode: str = "normal",
    position: Tuple[int, int] = (1, 1),
) -> Any:
    """Find or create a Merge compositing *foreground* over *background*."""
    merge = find_or_add_tool(comp, "Merge", name, position=position)
    connect(background, merge, "Background")
    connect(foreground, merge, "Foreground")
    set_merge_apply_mode(merge, apply_mode)
    return merge


def set_merge_apply_mode(merge: Any, mode: str) -> None:
    """Set a Merge's ``ApplyMode`` from a :data:`MERGE_APPLY_MODES` key.

    Unknown modes fall back to ``Normal`` rather than raising: a composite
    mode we can't honour should degrade to a plain alpha composite, not
    abort the whole build.
    """
    merge.SetInput("ApplyMode", MERGE_APPLY_MODES.get(mode, "Normal"))



# --------------------------------------------------------------------------- #
# Declarative transform animation
# --------------------------------------------------------------------------- #

class TransformAnimation:
    """Declarative description of a Transform tool's animation.

    Each field is an optional list of keyframes:

    * ``center``     — list of ``(frame, (x, y))``     in normalized coords
    * ``size``       — list of ``(frame, scalar)``     1.0 = original size
    * ``angle``      — list of ``(frame, degrees)``    counter-clockwise
    * ``pivot``      — list of ``(frame, (x, y))``     pivot for rotation/scale

    Frame values are relative to the start of the Fusion comp, which
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
    comp: Any, transform_tool: Any, animation: TransformAnimation
) -> None:
    """Push a :class:`TransformAnimation` onto a Transform tool.

    ``comp`` is required because animated inputs need a ``BezierSpline`` /
    ``XYPath`` modifier added to the same composition.
    """
    if animation.center:
        set_point_keyframes(comp, transform_tool, "Center", animation.center)
    if animation.pivot:
        set_point_keyframes(comp, transform_tool, "Pivot", animation.pivot)
    if animation.size:
        set_scalar_keyframes(comp, transform_tool, "Size", animation.size)
    if animation.angle:
        set_scalar_keyframes(comp, transform_tool, "Angle", animation.angle)


def attach_transform_animation(
    timeline_item: Any,
    animation: TransformAnimation,
    *,
    transform_name: str = DEFAULT_TRANSFORM_NAME,
) -> Any:
    """End-to-end helper: ensure a comp + Transform exist and apply *animation*.

    All edits land in the clip's *active* comp (the first comp, which is
    the one Resolve renders on the Edit-page timeline). After the
    Lock/Unlock pair, the comp is marked modified so Resolve picks up the
    change for playback.

    Returns the Transform tool.
    """
    comp = get_active_comp(timeline_item)
    with locked(comp):
        xform = insert_transform_chain(comp, transform_name=transform_name)
        apply_transform_animation(comp, xform, animation)
    mark_modified(comp)
    return xform


# --------------------------------------------------------------------------- #
# Tool inventory (mostly for diagnostics / live tests)
# --------------------------------------------------------------------------- #

def list_tools(comp: Any) -> List[str]:
    """Return the names of all tools in a comp (for diagnostics)."""
    tools = comp.GetToolList(False) or {}
    names: List[str] = []
    if isinstance(tools, dict):
        for key in sorted(tools.keys()):
            t = tools[key]
            try:
                names.append(t.GetAttrs("TOOLS_Name"))
            except Exception:
                try:
                    names.append(t.Name)
                except Exception:
                    names.append(str(key))
    return names


def _safe_name(timeline_item: Any) -> str:
    try:
        return timeline_item.GetName()
    except Exception:
        return "?"


__all__ = [
    "BLUR_SIZE_INPUT",
    "BLUR_TOOL",
    "DEFAULT_BACKGROUND_NAME",
    "DEFAULT_COLOR_BACKGROUND_NAME",
    "DEFAULT_COLOR_MERGE_NAME",
    "DEFAULT_BLUR_NAME",
    "DEFAULT_MERGE_NAME",
    "DEFAULT_PIXELATE_NAME",
    "DEFAULT_TRANSFORM_NAME",
    "MERGE_APPLY_MODES",
    "PIXELATE_SIZE_INPUT",
    "PIXELATE_TOOL",
    "PIXELATE_REFERENCE_WIDTH",
    "PIXELATE_MIN_FREQUENCY",
    "PIXELATE_MAX_FREQUENCY",
    "pixel_size_to_frequency",
    "TOOL_IMAGE_INPUTS",
    "PointKeyframe",
    "ScalarKeyframe",
    "TransformAnimation",
    "add_background",
    "add_blur",
    "add_merge",
    "add_pixelate",
    "apply_transform_animation",
    "attach_or_get_comp",
    "attach_transform_animation",
    "connect",
    "find_or_add_tool",
    "primary_image_input",
    "find_tool",
    "get_active_comp",
    "insert_tool_chain",
    "insert_transform_chain",
    "list_tools",
    "locked",
    "mark_modified",
    "set_constant",
    "set_merge_apply_mode",
    "set_point_keyframes",
    "set_scalar_keyframes",
]
