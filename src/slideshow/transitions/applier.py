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

    MediaIn1 ─► [Blur] ─► [Pixelate] ─► Transform ─┐
                                                   ├─► [DipMerge] ─┐
              Background(colour, opaque) ──────────┘               │
                                                                   ├─► MediaOut1
              Background(alpha 0) ─────────────────► [Merge] ──────┘

Both merges are optional. ``DipMerge`` only appears when the spec has a
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
    PIXELATE_SIZE_INPUT,
    PIXELATE_TOOL,
    PointKeyframe,
    ScalarKeyframe,
    TransformAnimation,
    add_background,
    add_merge,
    apply_transform_animation,
    connect,
    find_tool,
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


def _merge_transforms(
    lead_in: Optional[TransformAnimation],
    lead_out: Optional[TransformAnimation],
    lead_out_offset: int,
) -> Optional[TransformAnimation]:
    """Merge the four animated channels of two :class:`TransformAnimation`s."""
    if lead_in is None and lead_out is None:
        return None

    def channel(name):
        return _merge_keyframes(
            getattr(lead_in, name, None) if lead_in else None,
            getattr(lead_out, name, None) if lead_out else None,
            lead_out_offset,
        )

    merged = TransformAnimation(
        center=channel("center"),
        size=channel("size"),
        angle=channel("angle"),
        pivot=channel("pivot"),
    )
    return None if merged.is_empty() else merged


# --------------------------------------------------------------------------- #
# CompSpec
# --------------------------------------------------------------------------- #

@dataclass
class CompSpec:
    """Everything one clip's Fusion comp has to do, in clip-local frames.

    Produced by :func:`merge_clip_plans`; consumed by
    :func:`apply_comp_spec`. Mirrors :class:`ClipPlan` but with merged,
    absolute (clip-local) keyframes rather than transition-relative ones.
    """

    length_frames: int = 0
    transform: Optional[TransformAnimation] = None
    blend: Optional[List[ScalarKeyframe]] = None
    blur_size: Optional[List[ScalarKeyframe]] = None
    pixelate_size: Optional[List[ScalarKeyframe]] = None
    background_color: Optional[RgbColor] = None
    color_blend: Optional[List[ScalarKeyframe]] = None
    composite_mode: str = "normal"

    def is_empty(self) -> bool:
        """True when this comp would be a no-op (so we skip building it)."""
        return (
            (self.transform is None or self.transform.is_empty())
            and not self.blend
            and not self.blur_size
            and not self.pixelate_size
            and not self.color_blend
            and self.background_color is None
            and self.composite_mode == "normal"
        )


def merge_clip_plans(
    *,
    length_frames: int,
    lead_in: Optional[ClipPlan] = None,
    lead_out: Optional[ClipPlan] = None,
    lead_out_frames: int = 0,
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

    # composite_mode belongs to whichever half is on the upper track. A
    # mirrored plan moves it onto the lead-out, so honour both, preferring
    # the lead-in when they disagree.
    composite = "normal"
    if lead_in is not None and lead_in.composite_mode != "normal":
        composite = lead_in.composite_mode
    elif lead_out is not None and lead_out.composite_mode != "normal":
        composite = lead_out.composite_mode

    return CompSpec(
        length_frames=length_frames,
        transform=_merge_transforms(
            lead_in.transform if lead_in else None,
            lead_out.transform if lead_out else None,
            offset,
        ),
        blend=scalar("blend"),
        blur_size=scalar("blur_size"),
        pixelate_size=scalar("pixelate_size"),
        background_color=background,
        color_blend=scalar("color_blend"),
        composite_mode=composite,
    )


def comp_spec_for_clip(
    placed_clip: Any,
    *,
    lead_in_plan: Optional[TransitionPlan] = None,
    lead_out_plan: Optional[TransitionPlan] = None,
) -> CompSpec:
    """Convenience wrapper: build a CompSpec from a :class:`PlacedClip`.

    Picks the correct half of each neighbouring transition plan — the
    *incoming* half of the plan before the clip and the *outgoing* half of
    the plan after it.
    """
    return merge_clip_plans(
        length_frames=placed_clip.length_frames,
        lead_in=lead_in_plan.incoming if lead_in_plan is not None else None,
        lead_out=lead_out_plan.outgoing if lead_out_plan is not None else None,
        lead_out_frames=placed_clip.lead_out_frames,
    )


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

def build_comp_graph(comp: Any, spec: CompSpec) -> Dict[str, Any]:
    """Construct the node graph described by *spec* inside *comp*.

    Assumes the caller holds the comp lock. Returns the tools it created or
    reused, keyed by role (``transform``, ``blur``, ``pixelate``,
    ``background``, ``merge``) — handy for tests and diagnostics.
    """
    chain: List[Tuple[str, str]] = []
    if spec.blur_size:
        chain.append((BLUR_TOOL, DEFAULT_BLUR_NAME))
    if spec.pixelate_size:
        chain.append((PIXELATE_TOOL, DEFAULT_PIXELATE_NAME))
    chain.append(("Transform", DEFAULT_TRANSFORM_NAME))

    tools = insert_tool_chain(comp, chain)
    built: Dict[str, Any] = {"transform": tools[-1]}
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
    head = transform
    if spec.background_color is not None:
        color_background = add_background(
            comp,
            spec.background_color,
            name=DEFAULT_COLOR_BACKGROUND_NAME,
            alpha=1.0,
            position=(0, 2),
        )
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

    if spec.blend:
        background = add_background(
            comp,
            (0.0, 0.0, 0.0),
            name=DEFAULT_BACKGROUND_NAME,
            alpha=0.0,
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
    "PointKeyframe",
    "ScalarKeyframe",
    "TIMELINE_COMPOSITE_MODES",
    "apply_comp_spec",
    "apply_composite_mode",
    "build_comp_graph",
    "comp_spec_for_clip",
    "merge_clip_plans",
]
