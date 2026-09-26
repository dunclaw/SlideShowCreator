"""Apply :class:`~slideshow.transitions.base.TransitionPlan`s to Fusion comps.

The planning layer emits, for each transition, a ``ClipPlan`` for the
outgoing clip and one for the incoming clip. The layout layer places slides
so that adjacent pairs overlap on alternating tracks. Put those together and
**every clip carries two plans**:

* the ``incoming`` half of the transition *before* it — animating over
  clip-local frames ``[0, lead_in_frames]``;
* the ``outgoing`` half of the transition *after* it — animating over
  ``[length - lead_out_frames, length]``.

Both land in the clip's single Fusion comp, so they cannot simply be applied
one after the other: the second would overwrite the first's keyframes. They
are merged first into a :class:`CompSpec` (pure, testable), and only then
turned into a node graph.

The graph built from a CompSpec is::

    MediaIn1 ─► [Fit] ─┐
                       ├─► [CanvasMerge] ─► [Blur] ─► [Pixelate] ─► Transform ─┐
    Background(canvas) ┘                                                       │
                                                                               │
                                                     ├─► [DipMerge] ───────────┤
                   Background(colour, opaque) ───────┘                         │
                                                                               ├─► MediaOut1
                   Background(alpha 0) ─────────────────► [Merge] ─────────────┘

The canvas pair is what makes the comp render at the *timeline's* resolution
rather than the photograph's; it is upstream of everything so that effects and
animation work in frame pixels. See :func:`~slideshow.fusion_comps.add_canvas`.

The other merges are optional. ``DipMerge`` only appears when the spec has a
``background_color`` (dip-to-colour); ``Merge`` only when it has ``blend``.
Tools are only inserted when the spec needs them, and every tool has a
stable name so re-running the builder over the same timeline rewires rather
than duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..fusion_comps import (
    BLUR_SIZE_INPUT,
    BLUR_TOOL,
    DEFAULT_BACKGROUND_NAME,
    DEFAULT_BLUR_NAME,
    DEFAULT_COLOR_BACKGROUND_NAME,
    DEFAULT_COLOR_MERGE_NAME,
    DEFAULT_MERGE_NAME,
    DEFAULT_PIXELATE_NAME,
    DEFAULT_TRANSFORM_NAME,
    BACKDROP_KINDS,
    FRAMING_MODES,
    PIXELATE_SIZE_INPUT,
    PIXELATE_TOOL,
    PageTurnAnimation,
    PointKeyframe,
    ScalarKeyframe,
    TransformAnimation,
    add_background,
    add_canvas,
    add_image_average,
    add_merge,
    apply_transform_animation,
    build_page_turn_graph,
    connect,
    find_tool,
    framed_size,
    get_active_comp,
    insert_tool_chain,
    locked,
    mark_modified,
    pixel_size_to_frequency,
    set_scalar_keyframes,
)
from .base import ClipPlan, RgbColor, TransitionPlan


#: Resolve Edit-page ``CompositeMode`` values for the composite modes we
#: model. A per-clip Fusion comp cannot see the clip underneath it, so
#: additive / non-additive compositing between two *timeline* clips has to be
#: set at the Edit-page level instead of with a Fusion Merge.
#:
#: These are **integers**, not strings — ``SetProperty`` rejects strings and
#: says so only by returning ``False``. Resolve accepts any int in 0..31
#: without validating it, so the indices below were read off the Inspector on
#: Resolve Studio 20.3.3 rather than probed.
TIMELINE_COMPOSITE_MODES: Dict[str, int] = {
    "normal": 0,
    "add": 1,
    "non_add": 10,  # "Lighten" — max(fg, bg), the closest to non-additive
}


# --------------------------------------------------------------------------- #
# Keyframe merging
# --------------------------------------------------------------------------- #

def _shift(keyframes: Optional[Sequence[Any]], offset: int) -> List[Any]:
    """Return *keyframes* with every frame number moved by *offset*."""
    if not keyframes:
        return []
    return [(int(frame) + offset, value) for frame, value in keyframes]


def _merge_keyframes(
    lead_in: Optional[Sequence[Any]],
    lead_out: Optional[Sequence[Any]],
    lead_out_offset: int,
) -> Optional[List[Any]]:
    """Combine a clip's lead-in and lead-out keyframes for one input.

    The lead-out keys are shifted to their place near the end of the clip.
    When both halves are present and the lead-in doesn't leave the input at
    the value the lead-out expects to start from, a *hold* keyframe is
    inserted one frame before the lead-out starts — otherwise Fusion would
    interpolate across the whole quiet middle of the clip.

    Returns ``None`` when neither half animates this input.
    """
    if not lead_in and not lead_out:
        return None

    merged: Dict[int, Any] = {}
    for frame, value in _shift(lead_in, 0):
        merged[frame] = value

    shifted_out = _shift(lead_out, lead_out_offset)
    if lead_in and shifted_out:
        last_in_frame = max(merged)
        hold_value = merged[last_in_frame]
        first_out_frame, first_out_value = shifted_out[0]
        if hold_value != first_out_value and first_out_frame - 1 > last_in_frame:
            merged[first_out_frame - 1] = hold_value

    # Lead-out wins on any frame collision: it is the later edit in time.
    for frame, value in shifted_out:
        merged[frame] = value

    return [(frame, merged[frame]) for frame in sorted(merged)]


def _sample_at(keyframes: Sequence[Any], frame: int) -> Any:
    """Hold-clamped linear interpolation of *keyframes* at *frame*.

    Frames before the first or after the last keyframe hold that
    keyframe's value rather than extrapolating — every curve this is
    used on (transition halves and per-segment motion) is built to reach
    its neutral pose by its own last keyframe, so holding is equivalent
    to "this source is inactive outside its own time range".
    """
    ordered = sorted(keyframes, key=lambda kv: int(kv[0]))
    if frame <= int(ordered[0][0]):
        return ordered[0][1]
    if frame >= int(ordered[-1][0]):
        return ordered[-1][1]
    for (f0, v0), (f1, v1) in zip(ordered, ordered[1:]):
        f0, f1 = int(f0), int(f1)
        if f0 <= frame <= f1:
            if f1 == f0:
                return v1
            t = (frame - f0) / float(f1 - f0)
            if isinstance(v0, tuple):
                return tuple(a + (b - a) * t for a, b in zip(v0, v1))
            return v0 + (v1 - v0) * t
    return ordered[-1][1]


#: Neutral ("no effect") pose for each Transform channel, used to compose
#: multiple animated sources of the same channel — see :func:`_compose_channel`.
_CHANNEL_NEUTRAL: Dict[str, Any] = {
    "center": (0.5, 0.5),
    "pivot": (0.5, 0.5),
    "size": 1.0,
    "angle": 0.0,
}


def _compose_channel(
    name: str, sources: Sequence[Optional[Sequence[Any]]]
) -> Optional[List[Any]]:
    """Compose several animated curves for one Transform input into one.

    A transition's lead-in/lead-out and a clip's own motion can all
    animate the *same* channel (e.g. ``center`` for a ``slide_left``
    transition sharing a clip with a ``pan``) across overlapping time
    ranges. Splicing their keyframes together (picking whichever curve
    happens to own a given frame number) silently discards whichever
    curve loses the collision — in particular it was clobbering a
    transition's own entry keyframe at frame 0 with the motion's value,
    destroying the "enters from off-screen" animation entirely.

    Instead, every source is resampled at the union of all keyframe
    times and combined the way two independent transforms actually stack
    on the same image: translations (``center``/``pivot``/``angle``) add
    as offsets from the neutral pose, and scale (``size``) multiplies.
    """
    present = [list(source) for source in sources if source]
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    neutral = _CHANNEL_NEUTRAL[name]
    frames = sorted({int(frame) for source in present for frame, _ in source})
    result: List[Any] = []
    for frame in frames:
        samples = [_sample_at(source, frame) for source in present]
        if name == "size":
            combined: Any = 1.0
            for value in samples:
                combined *= value
        elif isinstance(neutral, tuple):
            combined = list(neutral)
            for value in samples:
                combined = [c + (v - n) for c, v, n in zip(combined, value, neutral)]
            combined = tuple(combined)
        else:
            combined = neutral + sum(value - neutral for value in samples)
        result.append((frame, combined))
    return result


def _merge_transforms(
    lead_in: Optional[TransformAnimation],
    lead_out: Optional[TransformAnimation],
    lead_out_offset: int,
    *,
    motion: Optional[TransformAnimation] = None,
) -> Optional[TransformAnimation]:
    """Merge the four animated channels of a clip's motion + transition transforms.

    The transition's own lead-in and lead-out never overlap in time (they
    sit at opposite ends of the clip with a static gap between), so those
    two are combined by straightforward concatenation via
    :func:`_merge_keyframes`. Motion, however, animates across the clip's
    *entire* length and so can be active at the same time as either
    transition half — that overlap has to be composed
    (:func:`_compose_channel`), not spliced, or one of the two curves gets
    silently overwritten wherever their keyframes collide.
    """
    if lead_in is None and lead_out is None and motion is None:
        return None

    def channel(name):
        transition_channel = _merge_keyframes(
            getattr(lead_in, name, None) if lead_in else None,
            getattr(lead_out, name, None) if lead_out else None,
            lead_out_offset,
        )
        motion_channel = getattr(motion, name, None) if motion else None
        return _compose_channel(name, (transition_channel, motion_channel))

    merged = TransformAnimation(
        center=channel("center"),
        size=channel("size"),
        angle=channel("angle"),
        pivot=channel("pivot"),
    )
    return None if merged.is_empty() else merged


def _merge_page_turns(
    lead_in: Optional[PageTurnAnimation],
    lead_out: Optional[PageTurnAnimation],
    lead_out_offset: int,
) -> Optional[PageTurnAnimation]:
    """Merge the page turn into and out of one clip.

    A clip has a single 3D scene, so the two halves have to share a hinge and
    a lens; the lead-in wins when they disagree. Under the split-track layout
    no segment ever carries both halves, so that tie-break is a safety net
    rather than something the builder relies on.
    """
    if lead_in is None and lead_out is None:
        return None
    angle = _merge_keyframes(
        lead_in.angle if lead_in else None,
        lead_out.angle if lead_out else None,
        lead_out_offset,
    )
    if not angle:
        return None
    source = lead_in if lead_in is not None else lead_out
    return PageTurnAnimation(
        angle=angle,
        hinge=source.hinge,
        focal_length=source.focal_length,
        cull_backface=source.cull_backface,
    )


# --------------------------------------------------------------------------- #
# CompSpec
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Framing:
    """How one photograph is sized onto the timeline frame.

    Carried on the :class:`CompSpec` because it is the *canvas* the rest of
    the graph animates in — see :func:`~slideshow.fusion_comps.add_canvas`
    for why a clip comp does not get that for free.

    ``backdrop_kind`` says what fills the area the photo does not reach.
    ``none`` (the default) leaves it transparent, reproducing the behaviour
    from before backdrops existed.
    """

    frame_width: int
    frame_height: int
    source_width: int
    source_height: int
    mode: str = "fit"
    backdrop_kind: str = "none"
    backdrop_color: RgbColor = (0.0, 0.0, 0.0)
    #: Prior photos for the ``accumulate`` pile, oldest first, as
    #: ``(path, width, height)`` or ``(path, width, height, pose)`` where
    #: ``pose`` is ``(center, size, angle, pivot)`` — the photo's final
    #: displayed transform, any of which may be ``None`` for identity.
    backdrop_sources: Tuple[Tuple[Any, ...], ...] = ()
    #: Composite the backdrop *inside* the clip's opacity fade. Set for upper
    #: track clips, whose backdrop must fade with them so the clip beneath
    #: (carrying an identical backdrop) shows through; lower-track clips keep
    #: theirs outside every fade so it never dims.
    backdrop_fades: bool = False

    def __post_init__(self) -> None:
        if self.mode not in FRAMING_MODES:
            raise ValueError(
                "mode must be one of {0}, got {1!r}".format(FRAMING_MODES, self.mode)
            )
        if self.backdrop_kind not in BACKDROP_KINDS:
            raise ValueError(
                "backdrop_kind must be one of {0}, got {1!r}".format(
                    BACKDROP_KINDS, self.backdrop_kind
                )
            )
        for label, value in (
            ("frame_width", self.frame_width),
            ("frame_height", self.frame_height),
            ("source_width", self.source_width),
            ("source_height", self.source_height),
        ):
            if value <= 0:
                raise ValueError("{0} must be > 0, got {1}".format(label, value))
        for source in self.backdrop_sources:
            if len(source) not in (3, 4):
                raise ValueError(
                    "backdrop source must be (path, width, height[, pose]), "
                    "got {0!r}".format(source)
                )
            path, width, height = source[0], source[1], source[2]
            if not path:
                raise ValueError("backdrop source path must not be empty")
            if width <= 0 or height <= 0:
                raise ValueError(
                    "backdrop source size must be positive, got {0}x{1}".format(
                        width, height
                    )
                )

    @property
    def backdrop_alpha(self) -> float:
        """Opacity of the canvas Background beneath everything else."""
        return 0.0 if self.backdrop_kind == "none" else 1.0

    def scaled_size(self) -> Tuple[int, int]:
        """Pixel size the photo is resampled to."""
        return framed_size(
            self.source_width,
            self.source_height,
            self.frame_width,
            self.frame_height,
            mode=self.mode,
        )

    def backdrop_size(self) -> Tuple[int, int]:
        """Pixel size the *backdrop* copy of the photo is resampled to.

        Always ``fill``: a backdrop that did not cover the frame would defeat
        the point of having one.
        """
        return framed_size(
            self.source_width,
            self.source_height,
            self.frame_width,
            self.frame_height,
            mode="fill",
        )

    def is_noop(self) -> bool:
        """True when building the canvas would change nothing on screen.

        A transparent ``fit`` canvas produces exactly what Resolve's own
        ``scaleToFit`` already does, so a clip that needs nothing else can
        still skip having a comp attached.
        """
        return self.mode == "fit" and self.backdrop_kind == "none"


@dataclass
class CompSpec:
    """Everything one clip's Fusion comp has to do, in clip-local frames.

    Produced by :func:`merge_clip_plans`; consumed by
    :func:`apply_comp_spec`. Mirrors :class:`ClipPlan` but with merged,
    absolute (clip-local) keyframes rather than transition-relative ones.
    """

    length_frames: int = 0
    transform: Optional[TransformAnimation] = None
    motion: Optional[TransformAnimation] = None
    blend: Optional[List[ScalarKeyframe]] = None
    blur_size: Optional[List[ScalarKeyframe]] = None
    pixelate_size: Optional[List[ScalarKeyframe]] = None
    background_color: Optional[RgbColor] = None
    color_blend: Optional[List[ScalarKeyframe]] = None
    background_from_image: bool = False
    composite_mode: str = "normal"
    page_turn: Optional[PageTurnAnimation] = None
    framing: Optional[Framing] = None

    def is_empty(self) -> bool:
        """True when this comp would be a no-op (so we skip building it)."""
        return (
            (self.transform is None or self.transform.is_empty())
            and (self.motion is None or self.motion.is_empty())
            and not self.blend
            and not self.blur_size
            and not self.pixelate_size
            and not self.color_blend
            and self.background_color is None
            and not self.background_from_image
            and self.composite_mode == "normal"
            and (self.page_turn is None or self.page_turn.is_empty())
            and (self.framing is None or self.framing.is_noop())
        )


def merge_clip_plans(
    *,
    length_frames: int,
    lead_in: Optional[ClipPlan] = None,
    lead_out: Optional[ClipPlan] = None,
    lead_out_frames: int = 0,
    motion: Optional[TransformAnimation] = None,
) -> CompSpec:
    """Merge a clip's incoming and outgoing halves into one :class:`CompSpec`.

    * ``length_frames``   — the clip's length on the timeline.
    * ``lead_in``         — ``TransitionPlan.incoming`` of the transition
      *into* this clip. Its frame 0 is the clip's frame 0.
    * ``lead_out``        — ``TransitionPlan.outgoing`` of the transition
      *out of* this clip. Its frame 0 sits at
      ``length_frames - lead_out_frames``.
    * ``lead_out_frames`` — the outgoing overlap length.

    Kept deliberately generic (it merges keyframe channels, not
    transitions) so per-clip motion can be folded in later as a third
    source without reworking this.
    """
    if length_frames < 0:
        raise ValueError(
            "length_frames must be >= 0, got {0}".format(length_frames)
        )
    if lead_out_frames < 0:
        raise ValueError(
            "lead_out_frames must be >= 0, got {0}".format(lead_out_frames)
        )
    offset = max(0, length_frames - lead_out_frames)

    def scalar(name):
        return _merge_keyframes(
            getattr(lead_in, name) if lead_in else None,
            getattr(lead_out, name) if lead_out else None,
            offset,
        )

    background = None
    if lead_in is not None and lead_in.background_color is not None:
        background = lead_in.background_color
    elif lead_out is not None and lead_out.background_color is not None:
        background = lead_out.background_color

    from_image = bool(
        (lead_in is not None and lead_in.background_from_image)
        or (lead_out is not None and lead_out.background_from_image)
    )

    # composite_mode belongs to whichever half is on the upper track. A
    # mirrored plan moves it onto the lead-out, so honour both, preferring
    # the lead-in when they disagree.
    composite = "normal"
    if lead_in is not None and lead_in.composite_mode != "normal":
        composite = lead_in.composite_mode
    elif lead_out is not None and lead_out.composite_mode != "normal":
        composite = lead_out.composite_mode

    page_turn = _merge_page_turns(
        lead_in.page_turn if lead_in else None,
        lead_out.page_turn if lead_out else None,
        offset,
    )

    merged_transform = _merge_transforms(
        lead_in.transform if lead_in else None,
        lead_out.transform if lead_out else None,
        offset,
        motion=motion,
    )
    return CompSpec(
        length_frames=length_frames,
        transform=merged_transform,
        motion=motion,
        blend=scalar("blend"),
        blur_size=scalar("blur_size"),
        pixelate_size=scalar("pixelate_size"),
        background_color=background,
        color_blend=scalar("color_blend"),
        background_from_image=from_image,
        composite_mode=composite,
        page_turn=page_turn,
    )


def comp_spec_for_clip(
    placed_clip: Any,
    *,
    lead_in_plan: Optional[TransitionPlan] = None,
    lead_out_plan: Optional[TransitionPlan] = None,
    framing: Optional[Framing] = None,
    motion: Optional[TransformAnimation] = None,
) -> CompSpec:
    """Convenience wrapper: build a CompSpec from a :class:`PlacedClip`.

    Picks the correct half of each neighbouring transition plan — the
    *incoming* half of the plan before the clip and the *outgoing* half of
    the plan after it.
    """
    spec = merge_clip_plans(
        length_frames=placed_clip.length_frames,
        lead_in=lead_in_plan.incoming if lead_in_plan is not None else None,
        lead_out=lead_out_plan.outgoing if lead_out_plan is not None else None,
        lead_out_frames=placed_clip.lead_out_frames,
        motion=motion,
    )
    spec.framing = framing
    return spec


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

def build_comp_graph(comp: Any, spec: CompSpec) -> Dict[str, Any]:
    """Construct the node graph described by *spec* inside *comp*.

    Assumes the caller holds the comp lock. Returns the tools it created or
    reused, keyed by role (``transform``, ``blur``, ``pixelate``,
    ``background``, ``merge``) — handy for tests and diagnostics.
    """
    if spec.page_turn is not None and not spec.page_turn.is_empty():
        # A page turn is a whole different pipeline: the image becomes a
        # texture on a 3D plane, so there is no 2D chain to hang a Blur or a
        # Transform off. ClipPlan rejects that combination at plan time.
        source = None
        render_size = None
        canvas = None
        if spec.framing is not None:
            media_in = find_tool(comp, "MediaIn1")
            if media_in is None:
                raise RuntimeError("Composition has no 'MediaIn1' tool.")
            canvas = add_canvas(
                comp,
                media_in,
                frame_size=(spec.framing.frame_width, spec.framing.frame_height),
                source_size=(spec.framing.source_width, spec.framing.source_height),
                mode=spec.framing.mode,
                backdrop=spec.framing.backdrop_kind,
                color=spec.framing.backdrop_color,
                backdrop_sources=spec.framing.backdrop_sources,
            )
            source = canvas["photo"]
            render_size = (spec.framing.frame_width, spec.framing.frame_height)
        built = build_page_turn_graph(
            comp,
            spec.page_turn,
            motion=spec.motion,
            source=source,
            render_size=render_size,
        )
        if canvas is not None:
            for role in (
                "fit",
                "canvas",
                "backdrop",
                "backdrop_fit",
                "backdrop_blur",
                "backdrop_merge",
            ):
                if role in canvas:
                    built[role] = canvas[role]
            built["canvas_merge"] = canvas["photo"]
            if spec.framing.backdrop_kind != "none":
                stable_merge = add_merge(
                    comp,
                    background=canvas["backdrop"],
                    foreground=built["renderer"],
                    name="SlideShowStableBackdropMerge",
                    apply_mode="normal",
                    position=(8, 0),
                )
                media_out = find_tool(comp, "MediaOut1")
                if media_out is None:
                    raise RuntimeError("Composition has no 'MediaOut1' tool.")
                connect(stable_merge, media_out, "Input")
                built["stable_backdrop_merge"] = stable_merge
        return built

    built: Dict[str, Any] = {}
    canvas = None

    # The canvas comes first, before any effect, so that everything
    # downstream animates in timeline pixels rather than in the
    # photograph's own undersized space.
    source: Any = None
    if spec.framing is not None:
        media_in = find_tool(comp, "MediaIn1")
        if media_in is None:
            raise RuntimeError("Composition has no 'MediaIn1' tool.")
        canvas = add_canvas(
            comp,
            media_in,
            frame_size=(spec.framing.frame_width, spec.framing.frame_height),
            source_size=(spec.framing.source_width, spec.framing.source_height),
            mode=spec.framing.mode,
            backdrop=spec.framing.backdrop_kind,
            color=spec.framing.backdrop_color,
            backdrop_sources=spec.framing.backdrop_sources,
        )
        for role in (
            "fit",
            "canvas",
            "backdrop",
            "backdrop_fit",
            "backdrop_blur",
            "backdrop_merge",
        ):
            if role in canvas:
                built[role] = canvas[role]
        built["canvas_merge"] = canvas["photo"]
        source = canvas["photo"]

    chain: List[Tuple[str, str]] = []
    if spec.blur_size:
        chain.append((BLUR_TOOL, DEFAULT_BLUR_NAME))
    if spec.pixelate_size:
        chain.append((PIXELATE_TOOL, DEFAULT_PIXELATE_NAME))
    chain.append(("Transform", DEFAULT_TRANSFORM_NAME))

    tools = insert_tool_chain(comp, chain, source=source)
    built["transform"] = tools[-1]
    index = 0
    if spec.blur_size:
        built["blur"] = tools[index]
        index += 1
    if spec.pixelate_size:
        built["pixelate"] = tools[index]

    transform = built["transform"]
    if spec.transform is not None and not spec.transform.is_empty():
        apply_transform_animation(comp, transform, spec.transform)
    if spec.blur_size:
        set_scalar_keyframes(comp, built["blur"], BLUR_SIZE_INPUT, spec.blur_size)
    if spec.pixelate_size:
        # Plans speak in block size; the OFX tool wants cells-across.
        set_scalar_keyframes(
            comp,
            built["pixelate"],
            PIXELATE_SIZE_INPUT,
            [(frame, pixel_size_to_frequency(value)) for frame, value in spec.pixelate_size],
        )

    # Opacity must come from a Merge, never from Transform.Blend.
    #
    # Fusion's ``Blend`` is a *universal* control that crossfades a tool's
    # input against its own output. On an identity Transform — which is what a
    # plain dissolve produces — input and output are the same image, so
    # animating Blend does precisely nothing. A Merge's Blend, by contrast,
    # controls foreground opacity against its background, which is a real fade.
    #
    # Dip-to-colour needs two of them stacked. The inner pair blends the
    # clip's own image against an opaque colour (so it can *become* the
    # colour); the outer pair blends that result against transparency (so it
    # can get out of the way of the clip on the track below). One merge could
    # only do one of those two things.
    #
    # Both Backgrounds below need an explicit size: without one they inherit
    # the comp's own frame format, which — per add_canvas's docstring — is
    # the photograph's native resolution inside a Resolve clip comp, not the
    # timeline's. A Merge takes its output size from its *background*, so an
    # undersized one here silently clips everything upstream (the
    # frame-sized canvas, the Transform's pan/zoom) down to the photo's own
    # pixel dimensions for as long as this clip is blending — then the next
    # clip's simpler graph (no such Merge) snaps back to the full, unclipped
    # frame the instant the transition ends.
    frame_size = (
        (spec.framing.frame_width, spec.framing.frame_height)
        if spec.framing is not None
        else None
    )
    head = transform
    has_backdrop = (
        spec.framing is not None
        and spec.framing.backdrop_kind != "none"
        and canvas is not None
    )
    if spec.background_from_image:
        # Same shape as the solid-colour dip, but the "colour" is a flat
        # field of the photograph's own average rather than a Background.
        media_in = find_tool(comp, "MediaIn1")
        if media_in is None:
            raise RuntimeError("Composition has no 'MediaIn1' tool.")
        average = add_image_average(
            comp, media_in, position=(0, 3), frame_size=frame_size
        )
        built["average_down"] = average["down"]
        built["average_up"] = average["up"]
        built["average_color"] = average["tint"]
        color_background = average["tint"]
    elif spec.background_color is not None:
        color_background = add_background(
            comp,
            spec.background_color,
            name=DEFAULT_COLOR_BACKGROUND_NAME,
            alpha=1.0,
            position=(0, 2),
            size=frame_size,
        )
    else:
        color_background = None

    if color_background is not None and has_backdrop:
        # With a persistent backdrop the dip must tint only the photo, never
        # the whole frame: a full-frame colour would hide the backdrop for
        # the whole transition and let it snap back when the next segment
        # starts. "Atop" keeps the colour inside the photo's own alpha, and
        # its Blend is the inverse of the plain dip's (colour, not photo).
        color_merge = add_merge(
            comp,
            background=head,
            foreground=color_background,
            name=DEFAULT_COLOR_MERGE_NAME,
            apply_mode="normal",
            position=(1, 2),
        )
        color_merge.SetInput("Operator", "Atop")
        built["color_background"] = color_background
        built["color_merge"] = color_merge
        head = color_merge
        if spec.color_blend:
            set_scalar_keyframes(
                comp,
                color_merge,
                "Blend",
                [(frame, 1.0 - value) for frame, value in spec.color_blend],
            )
        else:
            color_merge.SetInput("Blend", 0.0)
    elif color_background is not None:
        color_merge = add_merge(
            comp,
            background=color_background,
            foreground=head,
            name=DEFAULT_COLOR_MERGE_NAME,
            apply_mode="normal",
            position=(1, 2),
        )
        built["color_background"] = color_background
        built["color_merge"] = color_merge
        head = color_merge
        if spec.color_blend:
            set_scalar_keyframes(comp, color_merge, "Blend", spec.color_blend)

    def merge_stable_backdrop(foreground: Any) -> Any:
        stable_merge = add_merge(
            comp,
            background=canvas["backdrop"],
            foreground=foreground,
            name="SlideShowStableBackdropMerge",
            apply_mode="normal",
            position=(4, 1),
        )
        built["stable_backdrop_merge"] = stable_merge
        return stable_merge

    backdrop_fades = has_backdrop and spec.framing.backdrop_fades
    if backdrop_fades:
        head = merge_stable_backdrop(head)

    if spec.blend:
        background = add_background(
            comp,
            (0.0, 0.0, 0.0),
            name=DEFAULT_BACKGROUND_NAME,
            alpha=0.0,
            size=frame_size,
        )
        merge = add_merge(
            comp,
            background=background,
            foreground=head,
            name=DEFAULT_MERGE_NAME,
            apply_mode="normal",
        )
        built["background"] = background
        built["merge"] = merge
        head = merge
        set_scalar_keyframes(comp, merge, "Blend", spec.blend)

    if has_backdrop and not backdrop_fades:
        head = merge_stable_backdrop(head)

    if head is not transform:
        media_out = find_tool(comp, "MediaOut1")
        if media_out is None:
            raise RuntimeError("Composition has no 'MediaOut1' tool.")
        connect(head, media_out, "Input")

    return built


def apply_composite_mode(timeline_item: Any, mode: str) -> bool:
    """Set the Edit-page composite mode for additive / non-additive blends.

    A per-clip Fusion comp cannot see the clip beneath it, so ``add`` and
    ``non_add`` have to be expressed as a timeline-item property. Returns
    ``True`` when Resolve accepted the property.
    """
    if mode == "normal":
        return True
    value = TIMELINE_COMPOSITE_MODES.get(mode)
    if value is None:
        return False
    try:
        return bool(timeline_item.SetProperty("CompositeMode", value))
    except Exception:
        return False


def apply_comp_spec(timeline_item: Any, spec: CompSpec) -> Optional[Any]:
    """Realise *spec* on *timeline_item*'s active Fusion comp.

    No-ops (returning ``None``) for an empty spec, so clips that need no
    effects never get a comp attached — comps are not free, and a timeline
    of hard cuts should stay clean.

    Returns the comp handle that was edited.
    """
    if spec.is_empty():
        return None

    if spec.composite_mode != "normal":
        apply_composite_mode(timeline_item, spec.composite_mode)

    comp = get_active_comp(timeline_item)
    with locked(comp):
        build_comp_graph(comp, spec)
    mark_modified(comp)
    return comp


__all__ = [
    "CompSpec",
    "Framing",
    "PageTurnAnimation",
    "PointKeyframe",
    "ScalarKeyframe",
    "TIMELINE_COMPOSITE_MODES",
    "apply_comp_spec",
    "apply_composite_mode",
    "build_comp_graph",
    "comp_spec_for_clip",
    "merge_clip_plans",
]
