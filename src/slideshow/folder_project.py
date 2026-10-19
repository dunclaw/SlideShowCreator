"""Create slideshow data from a folder without contacting Resolve."""

import math
from pathlib import Path

from .project_model import SlideshowProject, TransitionChoice
from .transitions import registered_kinds


IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic"})


def gather_images(folder):
    """Return immediate image files in alphabetical order, as absolute paths."""
    directory = Path(folder).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("Not an image directory: {0}".format(directory))
    return [
        str(path)
        for path in sorted(directory.iterdir(), key=lambda path: path.name)
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS
    ]


def project_from_folder(folder, seconds, transition, name):
    """Validate dialog input before importing any media or creating a timeline."""
    name = name.strip()
    if not name:
        raise ValueError("Enter a timeline name.")
    try:
        seconds = float(seconds)
    except (ValueError, TypeError) as exc:
        raise ValueError("Slide duration must be a positive number of seconds.") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Slide duration must be a finite, positive number of seconds.")
    if transition not in ("auto", "none") and transition not in registered_kinds():
        raise ValueError("Unknown transition: {0}".format(transition))
    if not str(folder).strip():
        raise ValueError("Choose a folder of images.")
    paths = gather_images(folder)
    if not paths:
        raise ValueError("No supported images found in {0}.".format(folder))
    return SlideshowProject.from_paths(
        paths,
        name=name,
        default_item_duration_seconds=seconds,
        default_transition=TransitionChoice(kind=transition),
    )
