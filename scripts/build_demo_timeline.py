"""Integration demo: build a slideshow timeline from a folder of images.

Usage (Resolve must be running and have an open project):
    py -3.12 scripts\\build_demo_timeline.py <folder> [--seconds N] [--name "My Slideshow"]

Picks up all common image files in the folder, sorted alphabetically.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from slideshow.project_model import SlideshowProject  # noqa: E402
from slideshow.resolve_bridge import ResolveNotRunningError  # noqa: E402
from slideshow.timeline_builder import build_slideshow  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic"}


def _gather(folder: str) -> list:
    files = []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
            files.append(full)
    return files


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", help="Folder of images to add to the slideshow")
    p.add_argument("--seconds", type=float, default=4.0, help="Per-slide seconds")
    p.add_argument("--name", default="SlideShowCreator Demo", help="Timeline name")
    args = p.parse_args(argv)

    if not os.path.isdir(args.folder):
        print("Not a directory: {0}".format(args.folder), file=sys.stderr)
        return 2

    paths = _gather(args.folder)
    if not paths:
        print("No images found in {0}".format(args.folder), file=sys.stderr)
        return 2

    project = SlideshowProject.from_paths(
        paths, name=args.name, default_item_duration_seconds=args.seconds
    )

    try:
        timeline = build_slideshow(project)
    except ResolveNotRunningError as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 4

    print(
        "Built timeline {0!r} with {1} clips ({2:.1f}s each).".format(
            timeline.GetName(), len(paths), args.seconds
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
