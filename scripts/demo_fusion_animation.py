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
    """Animate Center.x from -0.5 (off-screen left) to 0.5 (centered).

    Uses an XYPath modifier with BezierSpline children for X and Y — the
    canonical Fusion pattern for animating Point inputs. Proven working on
    Resolve 20.3.
    """
    return fc.TransformAnimation(
        center=[
            (0, (-0.5, 0.5)),
            (duration_frames, (0.5, 0.5)),
        ],
    )


def _rotate_in(duration_frames: int) -> fc.TransformAnimation:
    """Rotate the image from 0 to 90 degrees over *duration_frames*.

    Uses only a scalar (Angle) keyframe — proven working on Resolve 20.3.
    """
    return fc.TransformAnimation(
        angle=[
            (0, 0.0),
            (duration_frames, 90.0),
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
        "--mode",
        choices=("slide", "angle"),
        default="angle",
        help="Animation to apply: 'angle' (rotate 0->90, scalar BezierSpline) "
             "or 'slide' (Center xy, XYPath modifier + child BezierSplines). "
             "Both are verified on Resolve 20.3.",
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
        # remove_comp was removed from the public API (Delete of the active
        # comp is unreliable in Resolve 20.3 and can crash the host). Just
        # report the comps so the user can delete them via the UI.
        names = clip.GetFusionCompNameList() or []
        print("Existing comps on clip: {0}".format(names))
        print("Delete them via the Fusion-comp dropdown / right-click menu.")
        return 0

    if args.mode == "slide":
        anim = _slide_in_from_left(args.frames)
        desc = "slide-in from left (Center x: -0.5 -> 0.5)"
    else:
        anim = _rotate_in(args.frames)
        desc = "rotate 0 -> 90 degrees (Angle scalar)"
    xform = fc.attach_transform_animation(clip, anim)
    print("Animation: {0}".format(desc))
    print("Attached comp; transform tool: {0!r}".format(
        getattr(xform, "Name", "?")
    ))

    comp = fc.get_active_comp(clip)
    print("Tools in comp: {0}".format(fc.list_tools(comp)))

    print()
    print("Verification:")
    print("  1. Switch to the Fusion page on this clip.")
    print("  2. You should see MediaIn1 -> SlideShowXf -> MediaOut1, plus")
    print("     a BezierSpline (Angle) or XYPath (Center) modifier.")
    print("  3. The clip should show the '3 yellow dots' modified marker.")
    print("  4. On the Edit page, scrub the first {0} frames — the image".format(args.frames))
    if args.mode == "slide":
        print("     should slide in from the left.")
    else:
        print("     should rotate from 0 to 90 degrees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
