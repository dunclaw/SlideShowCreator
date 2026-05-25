"""Project model for SlideShowCreator.

Pure-Python, no Resolve / Fusion dependencies — this module describes a
slideshow as in-memory data so it can be edited in the UI, persisted to
JSON, replayed by the builder, and unit-tested in isolation.

Top-level shape::

    SlideshowProject
    ├── name
    ├── default_item_duration_seconds      # used by items without an override
    ├── default_transition: TransitionChoice
    ├── default_motion: MotionChoice
    ├── audio: AudioSettings | None
    ├── target_total_duration_seconds: float | None  # bulk-duration target
    └── items: list[MediaItem]
        ├── path
        ├── duration_seconds: float | None     # per-item override
        ├── title: TitleSpec | None
        ├── motion: MotionChoice | None        # per-item Ken-Burns-style anim
        └── outgoing_transition: TransitionChoice | None
                                              # transition from THIS item to next

A media item carries **both** a transition (how it gives way to the next
clip) and a motion (animation applied for ~the entire duration of the
item — Movie-Maker-style pan / zoom). They're independent: an item can
have a zoom-in motion AND a cross-fade outgoing transition. Motions are
typically only meaningful for still images; the builder skips them on
video clips.

Transitions are attached to the *outgoing* edge of each item. Item N's
``outgoing_transition`` describes how N transitions into N+1. The last
item's ``outgoing_transition`` is ignored. A value of ``None`` (or kind
``"none"``) means a hard cut.

Backward-compat: the existing minimal API (``MediaItem(path=…,
duration_seconds=…, title_text=…)`` and
``SlideshowProject.from_paths(...)``) is preserved. ``title_text`` is
now a shortcut that materialises a ``TitleSpec`` with default
position/style.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #

#: All transition kinds we model. Roughly grouped:
#:
#: * Structural   — ``none`` (hard cut), ``auto`` (auto-mix picks).
#: * Dissolves    — ``dissolve`` and Resolve's native dissolve variants
#:   (``additive_dissolve``, ``non_additive_dissolve``, ``blur_dissolve``,
#:   ``dip_to_color`` — colour via ``params["color"] = "#RRGGBB"``,
#:   defaults to black). ``cross_fade`` is the Movie-Maker name for the
#:   standard dissolve and is kept as a distinct kind so saved projects
#:   show the user's chosen label.
#: * Fades        — ``fade`` (through black), ``fade_through_gray``.
#: * Effects      — ``blur_through_black``, ``pixelate``, ``smooth_cut``.
#: * Geometry     — ``slide_*`` (new clip slides in over old),
#:   ``push_*`` (both clips move together), ``zoom_in`` / ``zoom_out``
#:   (zoom-blur transition — NOT to be confused with the per-clip motion
#:   of the same name), ``flip``, ``drop``.
TRANSITION_KINDS: frozenset = frozenset({
    "none",
    "auto",
    "dissolve",
    "cross_fade",
    "additive_dissolve",
    "non_additive_dissolve",
    "blur_dissolve",
    "dip_to_color",
    "fade",
    "fade_through_gray",
    "blur_through_black",
    "pixelate",
    "smooth_cut",
    "slide_left", "slide_right", "slide_top", "slide_bottom",
    "push_left",  "push_right",  "push_top",  "push_bottom",
    "zoom_in",    "zoom_out",
    "flip",
    "drop",
})


DEFAULT_TRANSITION_DURATION_FRAMES = 24  # 1s at 24fps; UI can override


@dataclass
class TransitionChoice:
    """How one slide gives way to the next.

    * ``kind``: one of :data:`TRANSITION_KINDS`. ``"none"`` is a hard cut;
      ``"auto"`` defers the choice to the auto-mix planner.
    * ``duration_frames``: how many frames the two clips overlap. Must be
      ``>= 0`` (``0`` collapses to a hard cut).
    * ``params``: free-form ``dict[str, Any]`` for transition-specific
      tweaks (easing curve, motion-blur, drop-bounce factor, …). The
      transitions framework reads what it understands and ignores the
      rest, so adding new params never breaks old projects.
    """

    kind: str = "dissolve"
    duration_frames: int = DEFAULT_TRANSITION_DURATION_FRAMES
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in TRANSITION_KINDS:
            raise ValueError(
                "Unknown transition kind: {0!r}. Must be one of {1}.".format(
                    self.kind, sorted(TRANSITION_KINDS)
                )
            )
        if self.duration_frames < 0:
            raise ValueError(
                "TransitionChoice.duration_frames must be >= 0, got {0}".format(
                    self.duration_frames
                )
            )

    def is_cut(self) -> bool:
        """``True`` if this is effectively a hard cut (kind=none or 0 frames)."""
        return self.kind == "none" or self.duration_frames == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "duration_frames": int(self.duration_frames),
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransitionChoice":
        return cls(
            kind=str(data.get("kind", "dissolve")),
            duration_frames=int(
                data.get("duration_frames", DEFAULT_TRANSITION_DURATION_FRAMES)
            ),
            params=dict(data.get("params", {}) or {}),
        )


# --------------------------------------------------------------------------- #
# Titles
# --------------------------------------------------------------------------- #

TITLE_POSITIONS: frozenset = frozenset({"top", "center", "bottom"})


@dataclass
class TitleSpec:
    """Text overlay attached to a media item.

    * ``text``: the text to display.
    * ``position``: ``"top"`` / ``"center"`` / ``"bottom"``.
    * ``style``: name of the Fusion title template to use. ``"Text+"`` is
      the safe Resolve baseline; future versions may offer presets.
    * ``show_for_seconds``: how long to display the title. ``None`` = the
      entire clip duration.
    """

    text: str
    position: str = "center"
    style: str = "Text+"
    show_for_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.position not in TITLE_POSITIONS:
            raise ValueError(
                "TitleSpec.position must be one of {0}, got {1!r}".format(
                    sorted(TITLE_POSITIONS), self.position
                )
            )
        if self.show_for_seconds is not None and self.show_for_seconds <= 0:
            raise ValueError(
                "TitleSpec.show_for_seconds must be positive, got {0}".format(
                    self.show_for_seconds
                )
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "position": self.position,
            "style": self.style,
            "show_for_seconds": self.show_for_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TitleSpec":
        return cls(
            text=str(data.get("text", "")),
            position=str(data.get("position", "center")),
            style=str(data.get("style", "Text+")),
            show_for_seconds=_opt_float(data.get("show_for_seconds")),
        )


# --------------------------------------------------------------------------- #
# Motions (per-clip Ken-Burns-style animation)
# --------------------------------------------------------------------------- #

#: Motion families. Movie Maker offers ~30 named variants by combining
#: a kind + direction + rotation; we model the same shape parametrically.
#:
#: * ``none``     — no motion (image stays static).
#: * ``auto``     — auto-mix planner picks a kind/direction per clip.
#: * ``pan``      — constant zoom, image pans in ``direction``.
#: * ``zoom_in``  — image starts wide and zooms in toward ``direction``
#:                  (``"center"`` = straight zoom, no pan).
#: * ``zoom_out`` — image starts close and zooms out from ``direction``.
MOTION_KINDS: frozenset = frozenset({
    "none", "auto", "pan", "zoom_in", "zoom_out",
})

#: 8 cardinal directions + ``center``. ``center`` means "no directional
#: bias" — for pan it collapses to no motion (use ``kind="none"`` instead),
#: for zoom_in/zoom_out it means a straight axis-aligned zoom.
MOTION_DIRECTIONS: frozenset = frozenset({
    "center",
    "up", "down", "left", "right",
    "up_left", "up_right", "down_left", "down_right",
})


DEFAULT_MOTION_ZOOM_AMOUNT = 0.15  # 15% — gentle Ken Burns by default


@dataclass
class MotionChoice:
    """Per-clip animation applied for ~the entire clip duration.

    Modelled as ``(kind, direction)`` plus a couple of numeric tweaks so
    the ~30 named variants in Movie Maker's gallery can all be reproduced
    without exploding the type system:

    * ``kind``: one of :data:`MOTION_KINDS`.
    * ``direction``: one of :data:`MOTION_DIRECTIONS`. Ignored when
      ``kind`` is ``"none"`` or ``"auto"``.
    * ``zoom_amount``: how much to zoom over the clip's duration, as a
      fraction (``0.15`` = 15%). Used by ``pan`` (slight zoom to mask
      the pan's empty edges) and by ``zoom_in`` / ``zoom_out``.
    * ``rotation_degrees``: optional tilt during the move; Movie Maker
      has a few "rotated" zoom presets that use ~±5°.
    * ``duration_seconds``: explicit duration override. ``None`` = run
      for the full clip duration (the common case).
    * ``params``: free-form dict for transition-engine extensions
      (easing curve, focal point, …); unknown keys are ignored.
    """

    kind: str = "none"
    direction: str = "center"
    zoom_amount: float = DEFAULT_MOTION_ZOOM_AMOUNT
    rotation_degrees: float = 0.0
    duration_seconds: Optional[float] = None
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in MOTION_KINDS:
            raise ValueError(
                "Unknown motion kind: {0!r}. Must be one of {1}.".format(
                    self.kind, sorted(MOTION_KINDS)
                )
            )
        if self.direction not in MOTION_DIRECTIONS:
            raise ValueError(
                "Unknown motion direction: {0!r}. Must be one of {1}.".format(
                    self.direction, sorted(MOTION_DIRECTIONS)
                )
            )
        if self.zoom_amount < 0:
            raise ValueError(
                "MotionChoice.zoom_amount must be >= 0, got {0}".format(
                    self.zoom_amount
                )
            )
        if (
            self.duration_seconds is not None
            and self.duration_seconds <= 0
        ):
            raise ValueError(
                "MotionChoice.duration_seconds must be > 0 or None, got {0}".format(
                    self.duration_seconds
                )
            )

    def is_static(self) -> bool:
        """``True`` if this motion produces no animation."""
        return self.kind == "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "direction": self.direction,
            "zoom_amount": float(self.zoom_amount),
            "rotation_degrees": float(self.rotation_degrees),
            "duration_seconds": self.duration_seconds,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MotionChoice":
        return cls(
            kind=str(data.get("kind", "none")),
            direction=str(data.get("direction", "center")),
            zoom_amount=float(
                data.get("zoom_amount", DEFAULT_MOTION_ZOOM_AMOUNT)
            ),
            rotation_degrees=float(data.get("rotation_degrees", 0.0)),
            duration_seconds=_opt_float(data.get("duration_seconds")),
            params=dict(data.get("params", {}) or {}),
        )


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

SNAP_TARGETS: frozenset = frozenset({"beat", "downbeat", "onset"})


@dataclass
class AudioSettings:
    """Soundtrack + beat-sync configuration.

    * ``soundtrack_path``: absolute path to the audio file.
    * ``beat_sync_enabled``: when ``True``, the beat-sync engine snaps
      transition centres to the nearest analysis hit.
    * ``snap_to``: which analysis stream to snap to.
    * ``tolerance_seconds``: maximum allowable shift. If the nearest hit
      is further away than this, the original cut time is kept.
    * ``analysis_cache_path``: optional override for where the analyzer
      writes its JSON cache. Defaults to ``<soundtrack>.beatcache.json``
      handled by the cache module.
    """

    soundtrack_path: str
    beat_sync_enabled: bool = False
    snap_to: str = "beat"
    tolerance_seconds: float = 0.25
    analysis_cache_path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.snap_to not in SNAP_TARGETS:
            raise ValueError(
                "AudioSettings.snap_to must be one of {0}, got {1!r}".format(
                    sorted(SNAP_TARGETS), self.snap_to
                )
            )
        if self.tolerance_seconds < 0:
            raise ValueError(
                "AudioSettings.tolerance_seconds must be >= 0, got {0}".format(
                    self.tolerance_seconds
                )
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "soundtrack_path": self.soundtrack_path,
            "beat_sync_enabled": bool(self.beat_sync_enabled),
            "snap_to": self.snap_to,
            "tolerance_seconds": float(self.tolerance_seconds),
            "analysis_cache_path": self.analysis_cache_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AudioSettings":
        return cls(
            soundtrack_path=str(data.get("soundtrack_path", "")),
            beat_sync_enabled=bool(data.get("beat_sync_enabled", False)),
            snap_to=str(data.get("snap_to", "beat")),
            tolerance_seconds=float(data.get("tolerance_seconds", 0.25)),
            analysis_cache_path=_opt_str(data.get("analysis_cache_path")),
        )


# --------------------------------------------------------------------------- #
# Media items
# --------------------------------------------------------------------------- #

@dataclass(init=False)
class MediaItem:
    """A single piece of source media in the slideshow.

    * ``path``: absolute path to the source file.
    * ``duration_seconds``: per-item override of the slideshow default.
      ``None`` or non-positive values fall back to the project default.
    * ``title``: optional :class:`TitleSpec` overlay.
    * ``motion``: optional :class:`MotionChoice` per-clip animation
      (Movie-Maker-style pan / zoom). ``None`` means "use the
      slideshow's default_motion". Builder typically skips motion on
      video clips even if one is set.
    * ``outgoing_transition``: how this item transitions into the next.
      ``None`` means "use the slideshow's default_transition".
    * ``locked_duration``: when ``True``, bulk-duration adjustment leaves
      this item's duration alone.
    """

    path: str
    duration_seconds: Optional[float]
    title: Optional[TitleSpec]
    motion: Optional["MotionChoice"]
    outgoing_transition: Optional["TransitionChoice"]
    locked_duration: bool

    # Custom __init__ so we can accept the legacy ``title_text=`` kwarg
    # without breaking pre-existing callers / persisted JSON.
    def __init__(
        self,
        path: str,
        duration_seconds: Optional[float] = None,
        title: Optional[TitleSpec] = None,
        motion: Optional[MotionChoice] = None,
        outgoing_transition: Optional[TransitionChoice] = None,
        locked_duration: bool = False,
        title_text: Optional[str] = None,
    ) -> None:
        if title is not None and title_text is not None:
            raise TypeError(
                "Pass either title=TitleSpec(...) or title_text=str, not both."
            )
        self.path = path
        # Treat non-positive overrides as "no override" so the project
        # default still wins. Strict callers can validate themselves.
        if duration_seconds is not None and duration_seconds <= 0:
            self.duration_seconds = None
        else:
            self.duration_seconds = duration_seconds
        self.title = (
            TitleSpec(text=title_text) if title_text is not None else title
        )
        self.motion = motion
        self.outgoing_transition = outgoing_transition
        self.locked_duration = bool(locked_duration)

    @property
    def title_text(self) -> Optional[str]:
        return self.title.text if self.title is not None else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "duration_seconds": self.duration_seconds,
            "title": self.title.to_dict() if self.title is not None else None,
            "motion": (
                self.motion.to_dict() if self.motion is not None else None
            ),
            "outgoing_transition": (
                self.outgoing_transition.to_dict()
                if self.outgoing_transition is not None else None
            ),
            "locked_duration": bool(self.locked_duration),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaItem":
        title_data = data.get("title")
        title = TitleSpec.from_dict(title_data) if title_data else None
        if title is None and data.get("title_text"):
            title = TitleSpec(text=str(data["title_text"]))
        motion_data = data.get("motion")
        motion = MotionChoice.from_dict(motion_data) if motion_data else None
        trans_data = data.get("outgoing_transition")
        outgoing = (
            TransitionChoice.from_dict(trans_data) if trans_data else None
        )
        return cls(
            path=str(data.get("path", "")),
            duration_seconds=_opt_float(data.get("duration_seconds")),
            title=title,
            motion=motion,
            outgoing_transition=outgoing,
            locked_duration=bool(data.get("locked_duration", False)),
        )


# --------------------------------------------------------------------------- #
# Slideshow project
# --------------------------------------------------------------------------- #

PROJECT_SCHEMA_VERSION = 1


@dataclass
class SlideshowProject:
    """In-memory representation of a slideshow before it's pushed to Resolve."""

    name: str = "Slideshow"
    items: List[MediaItem] = field(default_factory=list)
    default_item_duration_seconds: float = 4.0
    default_transition: TransitionChoice = field(
        default_factory=lambda: TransitionChoice(kind="dissolve")
    )
    default_motion: MotionChoice = field(
        default_factory=lambda: MotionChoice(kind="none")
    )
    audio: Optional[AudioSettings] = None
    target_total_duration_seconds: Optional[float] = None

    @property
    def soundtrack_path(self) -> Optional[str]:
        return self.audio.soundtrack_path if self.audio is not None else None

    @soundtrack_path.setter
    def soundtrack_path(self, value: Optional[str]) -> None:
        if value is None:
            self.audio = None
        elif self.audio is None:
            self.audio = AudioSettings(soundtrack_path=value)
        else:
            self.audio.soundtrack_path = value

    @classmethod
    def from_paths(
        cls,
        paths: Sequence[str],
        *,
        name: str = "Slideshow",
        default_item_duration_seconds: float = 4.0,
        default_transition: Optional[TransitionChoice] = None,
        default_motion: Optional[MotionChoice] = None,
    ) -> "SlideshowProject":
        return cls(
            name=name,
            items=[MediaItem(path=p) for p in paths],
            default_item_duration_seconds=default_item_duration_seconds,
            default_transition=(
                default_transition
                if default_transition is not None
                else TransitionChoice(kind="dissolve")
            ),
            default_motion=(
                default_motion
                if default_motion is not None
                else MotionChoice(kind="none")
            ),
        )

    def effective_duration(self, item: MediaItem) -> float:
        """Duration in seconds Resolve should give *item* on the timeline."""
        if item.duration_seconds is not None and item.duration_seconds > 0:
            return item.duration_seconds
        return self.default_item_duration_seconds

    def transition_after(self, index: int) -> TransitionChoice:
        """Transition out of ``items[index]`` into ``items[index+1]``.

        Falls back to :attr:`default_transition` when the item has no
        override. Returns a hard-cut TransitionChoice when *index* is the
        last item (so callers can always iterate safely).
        """
        if index < 0 or index >= len(self.items):
            raise IndexError(
                "transition_after index out of range: {0} (have {1} items)".format(
                    index, len(self.items)
                )
            )
        if index == len(self.items) - 1:
            return TransitionChoice(kind="none", duration_frames=0)
        item = self.items[index]
        if item.outgoing_transition is not None:
            return item.outgoing_transition
        return self.default_transition

    def motion_for(self, index: int) -> MotionChoice:
        """Motion that should play on ``items[index]``.

        Falls back to :attr:`default_motion` when the item has no
        per-clip override.
        """
        if index < 0 or index >= len(self.items):
            raise IndexError(
                "motion_for index out of range: {0} (have {1} items)".format(
                    index, len(self.items)
                )
            )
        item = self.items[index]
        if item.motion is not None:
            return item.motion
        return self.default_motion

    def total_default_duration_seconds(self) -> float:
        """Sum of all items' effective durations (ignoring transition overlap)."""
        return sum(self.effective_duration(it) for it in self.items)

    def validate(self) -> List[str]:
        """Return a list of human-readable problems (empty = valid).

        Cheap structural checks only — no filesystem stat calls so this is
        safe to run from the UI on every edit.
        """
        problems: List[str] = []
        if self.default_item_duration_seconds <= 0:
            problems.append(
                "default_item_duration_seconds must be > 0 (got {0})".format(
                    self.default_item_duration_seconds
                )
            )
        if (
            self.target_total_duration_seconds is not None
            and self.target_total_duration_seconds <= 0
        ):
            problems.append(
                "target_total_duration_seconds must be > 0 or None (got {0})".format(
                    self.target_total_duration_seconds
                )
            )
        for i, item in enumerate(self.items):
            if not item.path:
                problems.append("items[{0}] has empty path".format(i))
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "name": self.name,
            "default_item_duration_seconds": float(
                self.default_item_duration_seconds
            ),
            "default_transition": self.default_transition.to_dict(),
            "default_motion": self.default_motion.to_dict(),
            "audio": self.audio.to_dict() if self.audio is not None else None,
            "target_total_duration_seconds": self.target_total_duration_seconds,
            "items": [it.to_dict() for it in self.items],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SlideshowProject":
        version = data.get("schema_version", 1)
        if int(version) > PROJECT_SCHEMA_VERSION:
            raise ValueError(
                "Project schema version {0} is newer than supported {1}. "
                "Upgrade SlideShowCreator to open this file.".format(
                    version, PROJECT_SCHEMA_VERSION
                )
            )
        default_trans_data = data.get("default_transition")
        default_trans = (
            TransitionChoice.from_dict(default_trans_data)
            if default_trans_data else TransitionChoice(kind="dissolve")
        )
        default_motion_data = data.get("default_motion")
        default_motion = (
            MotionChoice.from_dict(default_motion_data)
            if default_motion_data else MotionChoice(kind="none")
        )
        audio_data = data.get("audio")
        audio = AudioSettings.from_dict(audio_data) if audio_data else None
        if audio is None and data.get("soundtrack_path"):
            audio = AudioSettings(soundtrack_path=str(data["soundtrack_path"]))
        items_data = data.get("items") or []
        return cls(
            name=str(data.get("name", "Slideshow")),
            items=[MediaItem.from_dict(d) for d in items_data],
            default_item_duration_seconds=float(
                data.get("default_item_duration_seconds", 4.0)
            ),
            default_transition=default_trans,
            default_motion=default_motion,
            audio=audio,
            target_total_duration_seconds=_opt_float(
                data.get("target_total_duration_seconds")
            ),
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "SlideshowProject":
        return cls.from_dict(json.loads(text))

    def save_to_file(self, path: str, *, indent: int = 2) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json(indent=indent))

    @classmethod
    def load_from_file(cls, path: str) -> "SlideshowProject":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


__all__ = [
    "AudioSettings",
    "DEFAULT_MOTION_ZOOM_AMOUNT",
    "DEFAULT_TRANSITION_DURATION_FRAMES",
    "MOTION_DIRECTIONS",
    "MOTION_KINDS",
    "MediaItem",
    "MotionChoice",
    "PROJECT_SCHEMA_VERSION",
    "SNAP_TARGETS",
    "SlideshowProject",
    "TITLE_POSITIONS",
    "TRANSITION_KINDS",
    "TitleSpec",
    "TransitionChoice",
]
