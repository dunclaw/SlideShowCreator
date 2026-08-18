"""Live probe: composite modes, effect-tool input names, and clip placement.

Answers the three questions the transition applier had to guess at, against a
running DaVinci Resolve:

1. Does ``MediaPool.AppendToTimeline`` honour ``trackIndex`` / ``recordFrame``
   on a freshly created empty timeline?
2. What are the real input names on the Blur and Pixelate tools, and do our
   ``XBlurSize`` / ``XPixelSize`` guesses exist?
3. Can additive / non-additive compositing between two *timeline* clips be
   expressed at all — via ``TimelineItem.SetProperty("CompositeMode", …)``,
   since a per-clip Fusion comp cannot see the clip beneath it?

Usage (Resolve must be running with a project open):

    py -3.14 scripts\\probe_composite.py <folder-with-2-or-more-images>

Nothing is destroyed: the probe builds its own throwaway timeline. Findings
should be folded back into ``docs/api-notes.md``.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from slideshow.fusion_comps import (  # noqa: E402
    BLUR_SIZE_INPUT,
    PIXELATE_SIZE_INPUT,
    get_active_comp,
    locked,
    mark_modified,
)
from slideshow.project_model import SlideshowProject, TransitionChoice  # noqa: E402
from slideshow.resolve_bridge import ResolveNotRunningError  # noqa: E402
from slideshow.timeline_builder import build_slideshow  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

TIMELINE_COMPOSITE_CANDIDATES = [
    "Normal", "Add", "Lighten", "Screen", "Overlay", "Multiply",
]


def _report(label, value):
    print("  {0:<34} {1}".format(label + ":", value))


def _gather(folder):
    out = []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
            out.append(full)
    return out


def probe_placement(result):
    """Q1 — did the clips land where the layout asked for?"""
    print("\n[1] AppendToTimeline placement")
    for placed, item in zip(result.layout.clips, result.timeline_items):
        try:
            start = item.GetStart()
            end = item.GetEnd()
        except Exception as exc:
            _report("clip {0}".format(placed.index), "GetStart failed: {0}".format(exc))
            continue
        _report(
            "clip {0} wanted V{1}@{2}".format(
                placed.index, placed.track_index, placed.record_frame
            ),
            "got {0}..{1} ({2} frames)".format(start, end, end - start),
        )
    try:
        _report("timeline track count", result.timeline.GetTrackCount("video"))
    except Exception as exc:
        _report("timeline track count", "failed: {0}".format(exc))


def probe_tool_inputs(timeline_item):
    """Q2 — what inputs do Blur and Pixelate actually expose?"""
    print("\n[2] Blur / Pixelate input names")
    comp = get_active_comp(timeline_item)
    with locked(comp):
        for tool_type, guess in (
            ("Blur", BLUR_SIZE_INPUT),
            ("Pixelate", PIXELATE_SIZE_INPUT),
        ):
            tool = comp.AddTool(tool_type)
            if tool is None:
                _report(tool_type, "AddTool returned None")
                continue
            try:
                inputs = tool.GetInputList() or {}
                names = []
                for key in sorted(inputs.keys()):
                    try:
                        names.append(inputs[key].GetAttrs("INPS_ID"))
                    except Exception:
                        names.append(str(key))
            except Exception as exc:
                names = ["<GetInputList failed: {0}>".format(exc)]
            _report(tool_type + " inputs", ", ".join(str(n) for n in names))
            _report(
                "{0} has {1}".format(tool_type, guess),
                guess in names,
            )
    mark_modified(comp)


def probe_composite_modes(timeline_item):
    """Q3 — which Edit-page CompositeMode values does Resolve accept?"""
    print("\n[3] TimelineItem CompositeMode")
    try:
        current = timeline_item.GetProperty("CompositeMode")
    except Exception as exc:
        current = "<GetProperty failed: {0}>".format(exc)
    _report("current value", current)

    for value in TIMELINE_COMPOSITE_CANDIDATES:
        try:
            ok = timeline_item.SetProperty("CompositeMode", value)
        except Exception as exc:
            ok = "raised {0}".format(exc)
        try:
            readback = timeline_item.GetProperty("CompositeMode")
        except Exception:
            readback = "?"
        _report("SetProperty({0!r})".format(value), "{0} → {1}".format(ok, readback))

    # Leave the clip as we found it.
    try:
        timeline_item.SetProperty("CompositeMode", "Normal")
    except Exception:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", help="Folder with at least two images")
    args = p.parse_args(argv)

    paths = _gather(args.folder)[:3]
    if len(paths) < 2:
        print("Need at least two images in {0}".format(args.folder), file=sys.stderr)
        return 2

    project = SlideshowProject.from_paths(
        paths, name="SlideShowCreator Probe", default_item_duration_seconds=3.0
    )
    project.default_transition = TransitionChoice(kind="dissolve", duration_frames=24)

    try:
        result = build_slideshow(project, apply_transitions=False)
    except ResolveNotRunningError as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 3

    probe_placement(result)
    probe_tool_inputs(result.timeline_items[-1])
    probe_composite_modes(result.timeline_items[-1])
    print("\nDone. Fold the answers into docs/api-notes.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
