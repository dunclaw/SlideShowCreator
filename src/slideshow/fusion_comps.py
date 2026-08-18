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
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


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

#: The three tools of the average-colour chain, which stands in for the
#: solid Background when a dip takes its colour from the picture itself.
DEFAULT_AVERAGE_DOWN_NAME = "SlideShowAvgDown"
DEFAULT_AVERAGE_UP_NAME = "SlideShowAvgUp"
DEFAULT_AVERAGE_COLOR_NAME = "SlideShowAvgColor"

#: Fusion tool IDs. ``Blur`` is a native Fusion tool, but there is **no**
#: native pixelate tool — ``comp.AddTool("Pixelate")`` returns ``None``. The
#: effect is only available as a ResolveFX OFX plugin, addressed by its full
#: reverse-DNS ID. Verified against Resolve Studio 20.3.3.
BLUR_TOOL = "Blur"
PIXELATE_TOOL = "ofx.com.blackmagicdesign.resolvefx.MosaicBlur"

#: Fusion's resize tool. The plain ``Resize`` ID does *not* exist in Resolve
#: 20.3.3 — ``comp.AddTool("Resize")`` returns ``None``; the tool is
#: registered as ``BetterResize``.
RESIZE_TOOL = "BetterResize"

#: The canvas pair: a frame-sized Background and the Merge that composites the
#: fitted photograph onto it. See :func:`add_canvas`.
DEFAULT_CANVAS_NAME = "SlideShowCanvas"
DEFAULT_CANVAS_MERGE_NAME = "SlideShowCanvasMerge"
DEFAULT_FIT_NAME = "SlideShowFit"

#: Saturation and gamma applied to a photo's average colour before it is used
#: as a dip background.
#:
#: Averaging a whole photograph down to one pixel is a doubly destructive
#: operation. It is strongly *desaturating* — mixing every hue in the frame
#: together pulls the result toward grey — and it inherits the picture's
#: overall exposure, so a dim indoor shot averages to a murky near-black.
#: Left raw it produces exactly the "odd wash of grey" that the fixed mid-grey
#: dip suffers from, only darker.
#:
#: So we keep the *hue* the average identified and throw away its exposure.
#: Saturation pushes the surviving colour cast back up to something you can
#: actually name, and the gamma lift normalises brightness: it raises a dark
#: average a long way and a bright one barely at all, so every photo dips to a
#: comparably lit tint instead of the dip's brightness varying with the slide.
IMAGE_COLOR_SATURATION = 2.5
IMAGE_COLOR_GAMMA = 2.2

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


#: Recognised ways of sizing a photograph into the frame.
FRAMING_MODES = ("fit", "fill")


def framed_size(
    source_width: int,
    source_height: int,
    frame_width: int,
    frame_height: int,
    mode: str = "fit",
) -> Tuple[int, int]:
    """Pixel size a photo is resampled to before being laid on the canvas.

    ``fit`` scales until the photo is wholly inside the frame, leaving letter-
    or pillar-box bars for the backdrop to fill. ``fill`` scales until the
    frame is wholly covered, so the photo overhangs on one axis and gets
    cropped by the canvas.

    Note this returns the size of the *whole* photo in both modes — the crop
    in ``fill`` is done by the canvas merge, not by shrinking the image, which
    is what leaves room for a subject-aware offset later.
    """
    if mode not in FRAMING_MODES:
        raise ValueError(
            "mode must be one of {0}, got {1!r}".format(FRAMING_MODES, mode)
        )
    if source_width <= 0 or source_height <= 0:
        raise ValueError(
            "source size must be positive, got {0}x{1}".format(
                source_width, source_height
            )
        )
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError(
            "frame size must be positive, got {0}x{1}".format(
                frame_width, frame_height
            )
        )
    sx = frame_width / float(source_width)
    sy = frame_height / float(source_height)
    scale = min(sx, sy) if mode == "fit" else max(sx, sy)
    return (
        max(1, int(round(source_width * scale))),
        max(1, int(round(source_height * scale))),
    )


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
# 3D tool identifiers
# --------------------------------------------------------------------------- #

#: Fusion's 3D system. There is **no** native page-turn / page-fold tool in
#: either Fusion or ResolveFX (checked against all 395 registered tool IDs on
#: Resolve Studio 20.3.3), so a page turn has to be built out of these.
#:
#: ``Shape3D`` is used in preference to ``ImagePlane3D`` because it exposes
#: ``Width``/``Height`` explicitly — ``ImagePlane3D`` derives its size from the
#: image and gives no way to read it back, so fitting a camera to it is
#: guesswork. ``Shape3D`` also carries the subdivisions a ``Bender3D`` would
#: need to curl the page later.
SHAPE3D_TOOL = "Shape3D"
TRANSFORM3D_TOOL = "Transform3D"
CAMERA3D_TOOL = "Camera3D"
MERGE3D_TOOL = "Merge3D"
RENDERER3D_TOOL = "Renderer3D"

PAGE_PLANE_NAME = "SlideShowPage"
PAGE_TRANSFORM_NAME = "SlideShowPageXf"
PAGE_CAMERA_NAME = "SlideShowPageCam"
PAGE_MERGE_NAME = "SlideShowPageScene"
PAGE_RENDERER_NAME = "SlideShowPageRender"

#: 3D tool input names. The ``Transform3DOp.`` prefix is shared by every tool
#: that has a 3D transform (including ``Shape3D`` and ``Camera3D``).
SCENE_INPUT = "SceneInput"
MERGE3D_SCENE_INPUT_1 = "SceneInput1"
MERGE3D_SCENE_INPUT_2 = "SceneInput2"
PLANE_MATERIAL_INPUT = "MaterialInput"
TRANSFORM3D_ROTATE_Y = "Transform3DOp.Rotate.Y"
TRANSFORM3D_PIVOT_X = "Transform3DOp.Pivot.X"
TRANSFORM3D_TRANSLATE_Z = "Transform3DOp.Translate.Z"
PLANE_SIZE_LOCK = "SurfacePlaneInputs.SizeLock"
PLANE_WIDTH = "SurfacePlaneInputs.Width"
PLANE_HEIGHT = "SurfacePlaneInputs.Height"
PLANE_CULL_BACKFACE = "SurfacePlaneInputs.Visibility.CullBackFace"
PLANE_LIT = "SurfacePlaneInputs.Lighting.IsAffectedByLights"
CAMERA_FOCAL_LENGTH = "FLength"
CAMERA_AOV = "AoV"

#: Hinge edges a page can rotate about.
PAGE_HINGES: frozenset = frozenset({"left", "right"})

#: Default page-turn lens, in mm. Fusion's own default is 35mm, which is too
#: long to read as a fold — the page barely foreshortens. Shorter is wider and
#: more dramatic; below about 12mm the page distorts noticeably at the edges.
DEFAULT_PAGE_FOCAL_LENGTH = 18.0

#: How much further from the camera than its own width a page must sit. A page
#: hinged on a vertical edge reaches ``plane_width`` toward the camera at 90°,
#: so anything below 1.0 means it sweeps straight through the lens.
#:
#: 1.15 is deliberately just above the 1.136 that a letterboxed 4:3 photo gets
#: for free at 18mm — that case looks right as it is, so it stays untouched,
#: while a frame-filling page gets backed off by the smallest amount that
#: actually works. Raising this flattens the fold on wide pages for no visible
#: benefit.
PAGE_CAMERA_CLEARANCE = 1.15


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
    source: Optional[Any] = None,
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

    *source* substitutes a different tool for MediaIn1 at the head of the
    chain, which is how the canvas gets spliced in ahead of every effect.

    Returns the tools in the same order as *specs*.
    """
    upstream = source if source is not None else _require_tool(comp, media_in_name)
    media_out = _require_tool(comp, media_out_name)

    x, y = first_position
    tools: List[Any] = []
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
    size: Optional[Tuple[int, int]] = None,
) -> Any:
    """Find or create a solid-colour Background tool set to *color*.

    Fusion's Background tool exposes its colour as four separate scalar
    inputs (``TopLeftRed`` … ``TopLeftAlpha``) rather than one Point/RGBA
    input, so each channel is set individually.

    *size* forces the tool's canvas to an explicit pixel size. Without it a
    Background inherits the comp's frame format, and inside a Resolve
    timeline-clip comp that is the **photograph's** resolution, not the
    timeline's — see :func:`add_canvas`.
    """
    r, g, b = color
    tool = find_or_add_tool(comp, "Background", name, position=position)
    tool.SetInput("TopLeftRed", float(r))
    tool.SetInput("TopLeftGreen", float(g))
    tool.SetInput("TopLeftBlue", float(b))
    tool.SetInput("TopLeftAlpha", float(alpha))
    if size is not None:
        width, height = size
        # Has to be cleared first, or Width/Height are ignored.
        tool.SetInput("UseFrameFormatSettings", 0.0)
        tool.SetInput("Width", float(width))
        tool.SetInput("Height", float(height))
    return tool


def add_image_average(
    comp: Any,
    source: Any,
    *,
    saturation: float = IMAGE_COLOR_SATURATION,
    gamma: float = IMAGE_COLOR_GAMMA,
    position: Tuple[int, int] = (0, 3),
) -> Dict[str, Any]:
    """Build a full-frame flat field of *source*'s average colour.

    Used in place of :func:`add_background` when a dip should wash through
    the picture's own colour rather than a fixed one. Three tools::

        source ─> BetterResize(1x1) ─> BetterResize(frame) ─> BrightnessContrast

    Collapsing the image to a single pixel *is* the averaging step — a
    downscale that extreme has to combine every source pixel into the one
    output pixel — and scaling that pixel back up gives a flat field of the
    result. Doing it this way means we never have to decode the image in
    Python: the plug-in has to run under Resolve's bundled interpreter with
    the standard library only, so it has no way to read a JPEG, and Fusion
    is holding the decoded pixels anyway.

    The average is deliberately taken from the *raw* image rather than from
    the end of the 2D chain, so a transition that scales or moves the photo
    doesn't make the dip colour drift while it plays.

    Returns the three tools keyed ``down`` / ``up`` / ``tint``; ``tint`` is
    the one to composite against.
    """
    x, y = position

    down = find_or_add_tool(
        comp, RESIZE_TOOL, DEFAULT_AVERAGE_DOWN_NAME, position=(x, y)
    )
    # KeepAspect would quietly refuse the 1x1 request on a non-square frame,
    # and UseFrameFormatSettings would override Width/Height outright.
    down.SetInput("UseFrameFormatSettings", 0.0)
    down.SetInput("KeepAspect", 0.0)
    down.SetInput("Width", 1.0)
    down.SetInput("Height", 1.0)
    connect(source, down, "Input")

    up = find_or_add_tool(
        comp, RESIZE_TOOL, DEFAULT_AVERAGE_UP_NAME, position=(x + 1, y)
    )
    # Back to the comp's own frame format, so the Merge below gets a
    # background the same size as the foreground it has to cover.
    up.SetInput("KeepAspect", 0.0)
    up.SetInput("UseFrameFormatSettings", 1.0)
    connect(down, up, "Input")

    tint = find_or_add_tool(
        comp, "BrightnessContrast", DEFAULT_AVERAGE_COLOR_NAME, position=(x + 2, y)
    )
    tint.SetInput("Saturation", float(saturation))
    tint.SetInput("Gamma", float(gamma))
    connect(up, tint, "Input")

    return {"down": down, "up": up, "tint": tint}


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


def add_canvas(
    comp: Any,
    source: Any,
    *,
    frame_size: Tuple[int, int],
    source_size: Tuple[int, int],
    mode: str = "fit",
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    alpha: float = 0.0,
    position: Tuple[int, int] = (0, -2),
) -> Dict[str, Any]:
    """Resample *source* into the frame and lay it on a frame-sized canvas.

    Returns the tools created, keyed ``fit``, ``canvas`` and ``merge``; the
    ``merge`` is the new head of the chain.

    **Why this exists.** A Fusion comp attached to a Resolve timeline clip
    runs at the *clip's* resolution, not the timeline's: on a 3840x2160
    timeline a 1536x2048 photograph gives ``MediaIn1`` a 1536x2048 canvas,
    and Resolve letterboxes the comp's output afterwards according to the
    project's ``timelineInputResMismatchBehavior``. Two things follow, and
    both are bugs we actually shipped:

    * A Background added inside the comp covers only the photo's own box, so
      it can never paint the letterbox bars — there is nothing for a backdrop
      to be drawn on.
    * A Transform animates in that same undersized space, so a slide moves
      the photo across *its own* letterbox rather than across the frame, and
      the bars keep showing whatever is on the track below.

    So the canvas is established here, **upstream of every effect**, and from
    this point on the whole graph works in real frame pixels.

    With the default transparent black canvas the output is indistinguishable
    from letting Resolve do the fitting, which is what makes this safe to
    apply to every clip. Giving the canvas a colour and an alpha is then all
    a solid backdrop needs.
    """
    fit_w, fit_h = framed_size(
        source_size[0], source_size[1], frame_size[0], frame_size[1], mode=mode
    )
    resize = find_or_add_tool(comp, RESIZE_TOOL, DEFAULT_FIT_NAME, position=position)
    if resize is None:
        raise RuntimeError(
            "Could not create a {0!r} tool for the canvas fit.".format(RESIZE_TOOL)
        )
    resize.SetInput("Width", float(fit_w))
    resize.SetInput("Height", float(fit_h))
    connect(source, resize, primary_image_input(RESIZE_TOOL))

    canvas = add_background(
        comp,
        color,
        name=DEFAULT_CANVAS_NAME,
        alpha=alpha,
        position=(position[0], position[1] - 1),
        size=frame_size,
    )
    merge = add_merge(
        comp,
        background=canvas,
        foreground=resize,
        name=DEFAULT_CANVAS_MERGE_NAME,
        apply_mode="normal",
        position=(position[0] + 1, position[1] - 1),
    )
    return {"fit": resize, "canvas": canvas, "merge": merge}



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
# 3D page turn
# --------------------------------------------------------------------------- #

class PageTurnAnimation:
    """Declarative description of a rigid page rotating about one edge.

    * ``angle``        — list of ``(frame, degrees)`` about the Y (vertical)
                         axis. ``0`` is flat-on to camera and fills the frame
                         exactly; ``+90`` is edge-on with the free edge tipped
                         *toward* the camera; ``-90`` tips it away.
    * ``hinge``        — ``"right"`` (default) or ``"left"``: which edge of the
                         page stays put. US books are read left-to-right so
                         pages sweep right-to-left, which is a right hinge.
    * ``focal_length`` — camera focal length in mm. Shorter is wider, which
                         exaggerates the perspective and sells the fold.
                         The camera is always re-fitted to match, so this
                         changes the *feel* without changing the framing.
    * ``cull_backface``— hide the reverse of the page. Without it a page past
                         90 degrees shows a mirrored copy of its own image,
                         which reads as a glitch rather than a page.

    Frame numbers are clip-local, matching :class:`TransformAnimation`.
    """

    __slots__ = ("angle", "hinge", "focal_length", "cull_backface")

    def __init__(
        self,
        *,
        angle: Optional[Sequence[ScalarKeyframe]] = None,
        hinge: str = "right",
        focal_length: float = DEFAULT_PAGE_FOCAL_LENGTH,
        cull_backface: bool = True,
    ) -> None:
        if hinge not in PAGE_HINGES:
            raise ValueError(
                "PageTurnAnimation.hinge must be one of {0}, got {1!r}".format(
                    sorted(PAGE_HINGES), hinge
                )
            )
        if focal_length <= 0:
            raise ValueError(
                "PageTurnAnimation.focal_length must be > 0, got {0!r}".format(
                    focal_length
                )
            )
        self.angle = list(angle) if angle else None
        self.hinge = hinge
        self.focal_length = float(focal_length)
        self.cull_backface = bool(cull_backface)

    def is_empty(self) -> bool:
        return not self.angle

    def reversed_angle(self, duration_frames: int) -> List[ScalarKeyframe]:
        """``angle`` played backwards within a ``duration_frames`` window."""
        if not self.angle:
            return []
        flipped = [
            (duration_frames - int(frame), value) for frame, value in self.angle
        ]
        flipped.sort(key=lambda item: item[0])
        return flipped


def camera_distance(plane_height: float, aov_degrees: float) -> float:
    """Distance at which a plane of *plane_height* exactly fills the frame.

    Fusion's ``Camera3D`` reports a **vertical** angle of view (``AovType`` 0,
    derived from ``ApertureH``), and its default ``ResolutionGateFit`` is
    ``Height``, so fitting the height fits the width too as long as the plane's
    aspect matches the render's.
    """
    if plane_height <= 0:
        raise ValueError("plane_height must be > 0, got {0!r}".format(plane_height))
    half_aov = math.radians(float(aov_degrees)) / 2.0
    if half_aov <= 0:
        raise ValueError("aov_degrees must be > 0, got {0!r}".format(aov_degrees))
    return (plane_height / 2.0) / math.tan(half_aov)


def page_focal_length_for_clearance(
    focal_length: float,
    fitted_distance: float,
    plane_width: float,
    clearance: float = PAGE_CAMERA_CLEARANCE,
) -> float:
    """Lengthen *focal_length* until the swinging page clears the camera.

    The page hinges on a vertical edge and rotates about Y, which maps ``x``
    into ``z`` and leaves ``y`` alone — so the *whole* swing happens within
    ``plane_width`` of the pivot, and the free edge reaches its closest
    approach to the camera at 90°, at ``fitted_distance - plane_width``.

    When the plane is wider than the camera is distant that value goes
    negative: the page sweeps **through** the camera and out behind it, and
    the render dissolves into garbage — no fold, just the photo smearing and
    snapping. That is not an edge case. A 4:3 photo letterboxed into a 16:9
    frame is 1.333 wide against a distance of 1.515 and squeaks through with
    13% to spare, but a native 16:9 photo — or any photo once fill/crop
    framing is switched on — is 1.778 wide and does not.

    Backing the camera off alone would shrink the resting page, so we lengthen
    the lens instead: distance is exactly linear in focal length (both
    ``d = (h/2)/tan(aov/2)`` and ``tan(aov/2) = (apertureH/2)/F``, so
    ``d = h·F/apertureH``), which means scaling ``F`` scales ``d`` by the same
    factor and leaves the resting frame pixel-identical. The only cost is a
    gentler fold, and only for pages wide enough to need it.

    Returns *focal_length* unchanged when there is already enough room.
    """
    if focal_length <= 0:
        raise ValueError("focal_length must be > 0, got {0!r}".format(focal_length))
    if fitted_distance <= 0:
        raise ValueError(
            "fitted_distance must be > 0, got {0!r}".format(fitted_distance)
        )
    required = float(plane_width) * float(clearance)
    if required <= fitted_distance:
        return float(focal_length)
    return float(focal_length) * required / fitted_distance


def page_entry_angle(plane_width: float, distance: float) -> float:
    """Angle, in degrees, at which the page's free edge crosses the frame edge.

    A page swung far over is very close to the camera, and a point close to
    the camera projects a long way out: at 80° a frame-filling page sits
    entirely *off* the side of the screen, not edge-on in the middle of it as
    you would expect. It only slides into frame once its free edge comes back
    inside, and from there it covers the whole frame within another 20°.

    Working out where that happens: with the pivot on the right edge, the free
    edge sits at ``x = W/2 - W·cos(θ)``, ``z = W·sin(θ)``, the camera is at
    ``d``, and the frame's half-width at rest is ``W/2``. Setting the
    projected position equal to the frame edge,

    ``(W/2 - W·cos θ)·d = (d - W·sin θ)·(W/2)``

    collapses to ``tan θ = 2d/W`` — the height cancels out entirely, so this
    depends only on how wide the page is relative to the camera distance.

    Sweeping from any angle above this wastes frames on a page nobody can see
    and then dumps the whole fold into the last few, which reads as a hard
    wipe rather than a turn. Wider pages enter later: a letterboxed portrait
    photo enters at 76°, a frame-filling one at 66°.
    """
    if plane_width <= 0:
        raise ValueError("plane_width must be > 0, got {0!r}".format(plane_width))
    if distance <= 0:
        raise ValueError("distance must be > 0, got {0!r}".format(distance))
    return math.degrees(math.atan2(2.0 * distance, plane_width))


def fit_angles_to_frame(
    angle: List[ScalarKeyframe], entry_angle: float
) -> List[ScalarKeyframe]:
    """Rescale *angle* so its largest excursion is *entry_angle*.

    The transition plans the *shape* of the sweep — front-loaded, resting at
    exactly 0 on the last frame — without knowing the geometry it will be
    rendered against. Scaling here rather than at plan time keeps that split:
    the same plan produces a turn that uses all of its frames whether the page
    is a narrow portrait photo or fills the frame.

    Scaling, not clipping, so the eased profile is preserved and the page
    still comes to rest at exactly 0. A sweep already inside the visible range
    is returned untouched.
    """
    if not angle:
        return []
    peak = max(abs(value) for _, value in angle)
    if peak <= 0 or peak <= entry_angle:
        return list(angle)
    scale = entry_angle / peak
    return [(frame, value * scale) for frame, value in angle]


def build_page_turn_graph(
    comp: Any,
    animation: PageTurnAnimation,
    *,
    media_in_name: str = "MediaIn1",
    media_out_name: str = "MediaOut1",
) -> dict:
    """Replace *comp*'s image chain with a 3D page rotating about one edge.

    The graph is::

        MediaIn1 ─► Shape3D ─► Transform3D ─► Merge3D ─► Renderer3D ─► MediaOut1
                   (material)   (rotate Y)      ▲
                                            Camera3D

    Assumes the caller holds the comp lock.

    Two things make this fit the frame exactly, and both are easy to get
    wrong:

    * ``Shape3D``'s plane is **one world unit** square by default, *not*
      pixels/100. Its ``Width``/``Height`` are set explicitly here (with
      ``SizeLock`` cleared first, or they move together) to a unit-height
      rectangle matching the render aspect.
    * ``Renderer3D`` defaults its ``Width``/``Height`` to the *source* image
      resolution, not the timeline's. That is what the rest of the pipeline
      expects, so it is read back rather than overridden — but it means the
      plane aspect has to be derived from the renderer, not assumed 16:9.

    Get either wrong and the render still succeeds, it is just the wrong size
    — which looks like a mis-timed cut rather than a broken graph.

    Returns the tools it created or reused, keyed by role.
    """
    media_in = _require_tool(comp, media_in_name)
    media_out = _require_tool(comp, media_out_name)

    plane = find_or_add_tool(comp, SHAPE3D_TOOL, PAGE_PLANE_NAME, position=(1, 0))
    xform = find_or_add_tool(comp, TRANSFORM3D_TOOL, PAGE_TRANSFORM_NAME, position=(2, 0))
    camera = find_or_add_tool(comp, CAMERA3D_TOOL, PAGE_CAMERA_NAME, position=(2, 2))
    merge = find_or_add_tool(comp, MERGE3D_TOOL, PAGE_MERGE_NAME, position=(3, 0))
    renderer = find_or_add_tool(comp, RENDERER3D_TOOL, PAGE_RENDERER_NAME, position=(4, 0))

    connect(media_in, plane, PLANE_MATERIAL_INPUT)
    connect(plane, xform, SCENE_INPUT)
    connect(xform, merge, MERGE3D_SCENE_INPUT_1)
    connect(camera, merge, MERGE3D_SCENE_INPUT_2)
    connect(merge, renderer, SCENE_INPUT)
    connect(renderer, media_out, "Input")

    width = float(renderer.GetInput("Width") or 1920.0)
    height = float(renderer.GetInput("Height") or 1080.0)
    plane_height = 1.0
    plane_width = plane_height * (width / height if height else 1.0)

    plane.SetInput(PLANE_SIZE_LOCK, 0.0)
    plane.SetInput(PLANE_WIDTH, plane_width)
    plane.SetInput(PLANE_HEIGHT, plane_height)
    # No lights in the scene, so leave the texture unlit rather than let the
    # renderer fall back to its default lighting and darken the photo.
    plane.SetInput(PLANE_LIT, 0.0)
    plane.SetInput(PLANE_CULL_BACKFACE, 1.0 if animation.cull_backface else 0.0)

    camera.SetInput(CAMERA_FOCAL_LENGTH, animation.focal_length)
    aov = float(camera.GetInput(CAMERA_AOV) or 0.0)
    distance = camera_distance(plane_height, aov)
    # A page as wide as the camera is distant swings through the lens. Widen
    # the gap by lengthening the lens, which moves the camera back without
    # changing what a resting page looks like.
    focal_length = page_focal_length_for_clearance(
        animation.focal_length, distance, plane_width
    )
    if focal_length != animation.focal_length:
        camera.SetInput(CAMERA_FOCAL_LENGTH, focal_length)
        aov = float(camera.GetInput(CAMERA_AOV) or 0.0)
        distance = camera_distance(plane_height, aov)
    camera.SetInput(TRANSFORM3D_TRANSLATE_Z, distance)

    sign = 1.0 if animation.hinge == "right" else -1.0
    xform.SetInput(TRANSFORM3D_PIVOT_X, sign * plane_width / 2.0)
    entry_angle = page_entry_angle(plane_width, distance)
    angle = fit_angles_to_frame(animation.angle or [], entry_angle)
    if angle:
        set_scalar_keyframes(comp, xform, TRANSFORM3D_ROTATE_Y, angle)

    return {
        "plane": plane,
        "transform": xform,
        "camera": camera,
        "merge": merge,
        "renderer": renderer,
        "plane_size": (plane_width, plane_height),
        "render_size": (width, height),
        "aov": aov,
        "focal_length": focal_length,
        "camera_distance": distance,
        "entry_angle": entry_angle,
        "angle": angle,
    }


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
    "add_background",
    "add_blur",
    "add_canvas",
    "add_image_average",
    "add_merge",
    "add_pixelate",
    "apply_transform_animation",
    "attach_or_get_comp",
    "attach_transform_animation",
    "BLUR_SIZE_INPUT",
    "BLUR_TOOL",
    "build_page_turn_graph",
    "camera_distance",
    "connect",
    "DEFAULT_AVERAGE_COLOR_NAME",
    "DEFAULT_AVERAGE_DOWN_NAME",
    "DEFAULT_AVERAGE_UP_NAME",
    "DEFAULT_BACKGROUND_NAME",
    "DEFAULT_BLUR_NAME",
    "DEFAULT_CANVAS_MERGE_NAME",
    "DEFAULT_CANVAS_NAME",
    "DEFAULT_COLOR_BACKGROUND_NAME",
    "DEFAULT_COLOR_MERGE_NAME",
    "DEFAULT_FIT_NAME",
    "DEFAULT_MERGE_NAME",
    "DEFAULT_PAGE_FOCAL_LENGTH",
    "DEFAULT_PIXELATE_NAME",
    "DEFAULT_TRANSFORM_NAME",
    "find_or_add_tool",
    "find_tool",
    "fit_angles_to_frame",
    "framed_size",
    "FRAMING_MODES",
    "get_active_comp",
    "IMAGE_COLOR_GAMMA",
    "IMAGE_COLOR_SATURATION",
    "insert_tool_chain",
    "insert_transform_chain",
    "list_tools",
    "locked",
    "mark_modified",
    "MERGE_APPLY_MODES",
    "PAGE_CAMERA_CLEARANCE",
    "page_entry_angle",
    "page_focal_length_for_clearance",
    "PAGE_HINGES",
    "PageTurnAnimation",
    "pixel_size_to_frequency",
    "PIXELATE_MAX_FREQUENCY",
    "PIXELATE_MIN_FREQUENCY",
    "PIXELATE_REFERENCE_WIDTH",
    "PIXELATE_SIZE_INPUT",
    "PIXELATE_TOOL",
    "PointKeyframe",
    "primary_image_input",
    "RESIZE_TOOL",
    "ScalarKeyframe",
    "set_constant",
    "set_merge_apply_mode",
    "set_point_keyframes",
    "set_scalar_keyframes",
    "TOOL_IMAGE_INPUTS",
    "TransformAnimation",
]
