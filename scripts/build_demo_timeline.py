"""Integration demo: build a slideshow timeline from a folder of images.

Usage (Resolve must be running and have an open project):
    py -3.14 scripts\\build_demo_timeline.py <folder> [options]

Options of note:
    --transition KIND   transition between slides (default: dissolve).
                        Use ``--list-transitions`` to see them all.
    --frames N          transition length in frames (default: 24).
    --flat              build the old single-track, hard-cut timeline
                        instead — handy for A/B debugging.

Picks up all common image files in the folder, sorted alphabetically.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from slideshow.project_model import SlideshowProject, TransitionChoice  # noqa: E402
from slideshow.resolve_bridge import ResolveNotRunningError  # noqa: E402
from slideshow.timeline_builder import build_slideshow  # noqa: E402
from slideshow.transitions import registered_kinds  # noqa: E402

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
    p.add_argument("folder", nargs="?", help="Folder of images to add to the slideshow")
    p.add_argument("--seconds", type=float, default=4.0, help="Per-slide seconds")
    p.add_argument("--name", default="SlideShowCreator Demo", help="Timeline name")
    p.add_argument(
        "--transition", default="dissolve", help="Transition kind between slides"
    )
    p.add_argument(
        "--frames", type=int, default=24, help="Transition length in frames"
    )
    p.add_argument(
        "--flat",
        action="store_true",
        help="Build the legacy single-track timeline with hard cuts",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Use only the first N images (0 = all)",
    )
    p.add_argument(
        "--list-transitions",
        action="store_true",
        help="Print every available transition kind and exit",
    )
    args = p.parse_args(argv)

    if args.list_transitions:
        for kind in registered_kinds():
            print(kind)
        return 0

    if not args.folder:
        p.error("folder is required unless --list-transitions is given")
    if not os.path.isdir(args.folder):
        print("Not a directory: {0}".format(args.folder), file=sys.stderr)
        return 2

    paths = _gather(args.folder)
    if args.limit > 0:
        paths = paths[: args.limit]
    if not paths:
        print("No images found in {0}".format(args.folder), file=sys.stderr)
        return 2

    if args.transition not in registered_kinds() and args.transition != "none":
        print(
            "Unknown transition {0!r}. Known kinds: {1}".format(
                args.transition, ", ".join(registered_kinds())
            ),
            file=sys.stderr,
        )
        return 2

    project = SlideshowProject.from_paths(
        paths, name=args.name, default_item_duration_seconds=args.seconds
    )
    project.default_transition = TransitionChoice(
        kind=args.transition, duration_frames=args.frames
    )

    try:
        result = build_slideshow(project, overlap=not args.flat)
    except ResolveNotRunningError as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 4

    print(
        "Built timeline {0!r} with {1} clips ({2:.1f}s each).".format(
            result.timeline.GetName(), len(paths), args.seconds
        )
    )
    if result.layout is not None:
        print(
            "  layout: {0} frames on {1} video track(s), {2} transition(s) "
            "of {3!r}, {4} Fusion comp(s) written.".format(
                result.layout.total_frames,
                result.layout.track_count,
                len(result.transition_plans),
                args.transition,
                result.comps_applied,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
