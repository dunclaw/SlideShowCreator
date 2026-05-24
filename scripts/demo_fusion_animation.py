"""Live-demo: attach an animated Fusion Transform to one timeline clip.

Run this with DaVinci Resolve open and a timeline active::

    py -3.14 scripts/demo_fusion_animation.py
    py -3.14 scripts/demo_fusion_animation.py --track 1 --index 1
    py -3.14 scripts/demo_fusion_animation.py --clear

The demo attaches a Transform-tool comp to the chosen V1 clip that animates
the clip sliding in from the left over the first ~24 frames (configurable
with ``--frames``). After running it, switch to the Fusion page on that clip
and you should see a ``SlideShowXf`` Transform node with an animated ``Center``
input; on the Edit page, scrubbing through the first frames of the clip
should show the image flying in from the left.

This is the verification step for the ``fusion-helpers`` todo: if Center
animates, the whole transitions framework is unlocked.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# Make ``src/`` importable when running this script directly.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from slideshow import fusion_comps as fc
from slideshow.resolve_bridge import ResolveContext, ResolveNotRunningError


def _pick_clip(timeline, track: int, index: int):
    clips = timeline.GetItemListInTrack("video", track) or []
    if not clips:
        raise SystemExit(
            "Timeline track V{0} has no clips.".format(track)
        )
    if index < 1 or index > len(clips):
        raise SystemExit(
            "Clip index {0} out of range (V{1} has {2} clip(s)).".format(
                index, track, len(clips)
            )
        )
    return clips[index - 1]


def _slide_in_from_left(duration_frames: int) -> fc.TransformAnimation:
    """Animate Center.x from -0.5 (off-screen left) to 0.5 (centered)."""
    return fc.TransformAnimation(
        center=[
            (0, (-0.5, 0.5)),
            (duration_frames, (0.5, 0.5)),
        ],
        size=[
            (0, 1.0),
        ],
        angle=[
            (0, 0.0),
        ],
    )


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--track", type=int, default=1, help="Video track index (1-based).")
    p.add_argument("--index", type=int, default=1, help="Clip index within the track (1-based).")
    p.add_argument(
        "--frames",
        type=int,
        default=24,
        help="Slide-in duration in frames (default 24 = 1s at 24fps).",
    )
    p.add_argument(
        "--clear",
        action="store_true",
        help="Remove the SlideShowCreator comp from the chosen clip instead of adding it.",
    )
    args = p.parse_args(argv)

    try:
        ctx = ResolveContext()
        resolve = ctx.resolve
    except ResolveNotRunningError as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 2

    print("Connected to: {0} {1}".format(
        resolve.GetProductName(), resolve.GetVersionString()
    ))

    project = ctx.project
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        print("FAIL: No current timeline. Create or open one and retry.", file=sys.stderr)
        return 3
    print("Timeline: {0!r}".format(timeline.GetName()))

    clip = _pick_clip(timeline, args.track, args.index)
    print("Target clip: V{0} item {1}: {2!r}".format(
        args.track, args.index, clip.GetName()
    ))

    if args.clear:
        removed = fc.remove_comp(clip)
        print("Cleared SlideShowCreator comp." if removed else
              "No SlideShowCreator comp to clear.")
        return 0

    anim = _slide_in_from_left(args.frames)
    xform = fc.attach_transform_animation(clip, anim)
    print("Attached comp; transform tool: {0!r}".format(
        getattr(xform, "Name", "?")
    ))

    comp = fc.attach_or_get_comp(clip)
    print("Tools in comp: {0}".format(fc.list_tools(comp)))

    print()
    print("Verification:")
    print("  1. Switch to the Fusion page (or click the clip and press Shift+5).")
    print("  2. You should see MediaIn1 -> SlideShowXf -> MediaOut1.")
    print("  3. Click SlideShowXf and look at the Center input — it should be")
    print("     animated (green keyframe markers in the inspector).")
    print("  4. On the Edit page, scrub the first {0} frames — the image".format(args.frames))
    print("     should slide in from the left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
