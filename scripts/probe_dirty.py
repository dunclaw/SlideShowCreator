"""
probe_dirty.py - Find the API call that marks a comp as modified so
Resolve will actually render our script edits on the edit-page timeline.

Background: probe_active.py proved that editing the FIRST comp on a clip
via LoadFusionCompByName's return value DOES land in the right place
with correct keyframes. But the edit-page timeline did not show the
animation until the user manually dragged a node in the Fusion UI,
which made the "3 yellow dots" modified marker appear on the clip.

Hypothesis: Resolve only re-renders a Fusion comp when its
COMPB_Modified attribute flips to True. Our script edits via
PyRemoteObject don't trip that flag.

This probe runs three candidates IN ORDER on three different clips
(V1 #1, #2, #3 by default). User then checks which clips show the
animation on edit-page playback AND which clips show the 3 yellow dots.

USER MUST: have at least 3 clips on V1. Each test clip should have
exactly one comp ('Composition 1') with no nodes. Easiest setup:
duplicate a freshly-cleaned test clip three times in the timeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from slideshow.resolve_bridge import ResolveContext


def edit_comp(clip, label, tool_name):
    """Add Transform + animated Angle to the FIRST comp on this clip."""
    existing = clip.GetFusionCompNameList() or []
    if not existing:
        clip.AddFusionComp()
        existing = clip.GetFusionCompNameList() or []
    target_name = existing[0]
    comp = clip.LoadFusionCompByName(target_name)
    if not comp:
        print(f"  [{label}] LoadFusionCompByName returned falsy; skipping")
        return None

    mi = comp.FindTool("MediaIn1")
    mo = comp.FindTool("MediaOut1")
    comp.Lock()
    try:
        xf = comp.AddTool("Transform")
        xf.SetAttrs({"TOOLS_Name": tool_name})
        if mi is not None:
            xf.ConnectInput("Input", mi.Output)
        if mo is not None:
            mo.ConnectInput("Input", xf.Output)
        spline = comp.AddTool("BezierSpline")
        spline.SetKeyFrames({0: [0.0], 24: [90.0]})
        xf.ConnectInput("Angle", spline)
    finally:
        comp.Unlock()
    return comp


def try_call(label, fn, *args, **kwargs):
    print(f"  -> {label} ... ", end="")
    try:
        r = fn(*args, **kwargs)
        print(f"ok ({r!r})")
        return r
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    ctx = ResolveContext()
    resolve = ctx.resolve
    timeline = ctx.current_timeline
    print(f"Connected: {resolve.GetProductName()} {resolve.GetVersionString()}")
    print(f"Timeline:  {timeline.GetName()!r}")

    items = timeline.GetItemListInTrack("video", 1) or []
    print(f"V1 has {len(items)} clip(s)")
    if len(items) < 3:
        print("Need at least 3 clips on V1. Abort.")
        return 1

    # ---- Candidate 1: nothing extra (control) ----
    print()
    print("=== CANDIDATE 1: BASELINE (no dirty call) on V1 #1 ===")
    clip1 = items[0]
    print(f"  clip: {clip1.GetName()!r}")
    edit_comp(clip1, "C1", "XF_BASELINE")
    print("  no dirty call applied")

    # ---- Candidate 2: SetAttrs COMPB_Modified=True ----
    print()
    print("=== CANDIDATE 2: comp.SetAttrs(COMPB_Modified=True) on V1 #2 ===")
    clip2 = items[1]
    print(f"  clip: {clip2.GetName()!r}")
    comp2 = edit_comp(clip2, "C2", "XF_SETATTRS")
    if comp2 is not None:
        try_call("comp.GetAttrs('COMPB_Modified') BEFORE",
                 comp2.GetAttrs, "COMPB_Modified")
        try_call("comp.SetAttrs({'COMPB_Modified': True})",
                 comp2.SetAttrs, {"COMPB_Modified": True})
        try_call("comp.GetAttrs('COMPB_Modified') AFTER",
                 comp2.GetAttrs, "COMPB_Modified")

    # ---- Candidate 3: comp.Save() to comp's filename, or comp.Save("...") ----
    print()
    print("=== CANDIDATE 3: comp.Save() on V1 #3 ===")
    clip3 = items[2]
    print(f"  clip: {clip3.GetName()!r}")
    comp3 = edit_comp(clip3, "C3", "XF_SAVE")
    if comp3 is not None:
        # try parameterless Save first (autosave into current comp)
        try_call("comp.Save()", comp3.Save)
        try_call("comp.GetAttrs('COMPB_Modified') after Save",
                 comp3.GetAttrs, "COMPB_Modified")

    # ---- Bonus: if 4th clip exists, try BOTH ----
    if len(items) >= 4:
        print()
        print("=== CANDIDATE 4: SetAttrs + Save combined on V1 #4 ===")
        clip4 = items[3]
        print(f"  clip: {clip4.GetName()!r}")
        comp4 = edit_comp(clip4, "C4", "XF_BOTH")
        if comp4 is not None:
            try_call("comp.SetAttrs COMPB_Modified=True",
                     comp4.SetAttrs, {"COMPB_Modified": True})
            try_call("comp.Save()", comp4.Save)

    # ---- Bonus 2: introspect dir(comp) for any other promising calls ----
    print()
    print("=== dir(comp) candidates (Save/Render/Refresh/Modified/Update) ===")
    if comp2 is not None:
        try:
            d = dir(comp2)
            for name in d:
                lname = name.lower()
                if any(k in lname for k in ("save", "render", "refresh",
                                            "modif", "update", "flush",
                                            "commit", "dirty")):
                    print(f"  - {name}")
        except Exception as exc:
            print(f"  dir() failed: {exc}")

    print()
    print("=" * 60)
    print("Now check each clip on the EDIT page:")
    print("  - Does playback show rotation 0->90?")
    print("  - Does the clip show the '3 yellow dots' modified marker?")
    print()
    print("Clips:")
    print("  V1 #1 (XF_BASELINE)   - baseline (no dirty call)")
    print("  V1 #2 (XF_SETATTRS)   - SetAttrs COMPB_Modified=True")
    print("  V1 #3 (XF_SAVE)       - comp.Save()")
    if len(items) >= 4:
        print("  V1 #4 (XF_BOTH)       - both")
    return 0


if __name__ == "__main__":
    sys.exit(main())
