"""
probe_active.py - Test whether the FIRST/ONLY comp on a clip is the
"active" one used for timeline rendering.

Hypothesis: previous probes saw nodes in the Fusion preview but not on
edit-page playback because each clip had multiple comps and Resolve
only renders the FIRST/ACTIVE one. Approach X (edit through
LoadFusionCompByName's return value) successfully wrote to the right
clip, but we always added a NEW comp instead of editing the active one.

This probe:
1. Picks the FIRST clip on V1 of the current timeline (no clip selection
   needed - operates on item by index).
2. If the clip has NO existing comps: AddFusionComp + take its name.
3. Else: take the FIRST existing comp name (assumed active).
4. Loads it via LoadFusionCompByName and edits through that handle.
5. Adds Transform with Angle keyframes 0->90 over 24 frames.

USER MUST:
- Use a fresh timeline with a fresh clip (preferably zero existing comps),
  OR delete any prior probe comps from clip #1 before running.
- After running, scrub/play clip #1 on the EDIT page. Should see the
  image rotate 0->90 degrees over the first second.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from slideshow.resolve_bridge import ResolveContext


def main() -> int:
    ctx = ResolveContext()
    resolve = ctx.resolve
    fusion = ctx.fusion
    project = ctx.project
    timeline = ctx.current_timeline
    print(f"Connected: {resolve.GetProductName()} {resolve.GetVersionString()}")
    print(f"Timeline:  {timeline.GetName()!r}")

    items = timeline.GetItemListInTrack("video", 1) or []
    if not items:
        print("No V1 clips found.")
        return 1
    clip = items[0]
    print(f"Target clip: V1 #1 of {len(items)}: {clip.GetName()!r}")
    print(f"  start={clip.GetStart()} duration={clip.GetDuration()} frames")

    existing = clip.GetFusionCompNameList() or []
    print(f"  existing comps: {existing}")

    if not existing:
        new_name = clip.AddFusionComp()
        print(f"  no comps; added: {new_name!r}")
        target_name = new_name
    else:
        target_name = existing[0]
        print(f"  using FIRST existing comp (assumed active): {target_name!r}")

    # APPROACH X: edit through LoadFusionCompByName's return value
    comp = clip.LoadFusionCompByName(target_name)
    print(f"  LoadFusionCompByName -> {comp}")
    if not comp:
        print("ERROR: LoadFusionCompByName returned falsy")
        return 1

    tools_before = comp.GetToolList(False) or {}
    print("  tools BEFORE:")
    for i in sorted(tools_before):
        t = tools_before[i]
        try:
            name = t.GetAttrs("TOOLS_Name")
        except Exception:
            name = "?"
        print(f"    {i} {name!r}")

    mi = comp.FindTool("MediaIn1")
    mo = comp.FindTool("MediaOut1")
    print(f"  MediaIn1: {mi}, MediaOut1: {mo}")

    # If a SlideShowXf already exists from a previous run, reuse it; else add
    xf = comp.FindTool("SlideShowXf")
    if xf is None:
        comp.Lock()
        try:
            xf = comp.AddTool("Transform")
            xf.SetAttrs({"TOOLS_Name": "SlideShowXf"})
            if mi is not None:
                xf.ConnectInput("Input", mi.Output)
            if mo is not None:
                mo.ConnectInput("Input", xf.Output)
            spline = comp.AddTool("BezierSpline")
            spline.SetKeyFrames({0: [0.0], 24: [90.0]})
            xf.ConnectInput("Angle", spline)
        finally:
            comp.Unlock()
    else:
        print("  SlideShowXf already exists; updating keyframes only")
        comp.Lock()
        try:
            # Find existing spline modifier or create new
            spline_name = "SlideShowXfAngle"
            spline = comp.FindTool(spline_name)
            if spline is None:
                spline = comp.AddTool("BezierSpline")
                xf.ConnectInput("Angle", spline)
            spline.SetKeyFrames({0: [0.0], 24: [90.0]})
        finally:
            comp.Unlock()

    print("  tools AFTER:")
    tools_after = comp.GetToolList(False) or {}
    for i in sorted(tools_after):
        t = tools_after[i]
        try:
            name = t.GetAttrs("TOOLS_Name")
        except Exception:
            name = "?"
        print(f"    {i} {name!r}")

    # Read back animation values
    xf2 = comp.FindTool("SlideShowXf")
    if xf2 is not None:
        print("  Angle read-back via comp handle:")
        for f in (0, 6, 12, 18, 24):
            try:
                v = xf2.GetInput("Angle", f)
            except Exception as exc:
                v = f"ERR: {exc}"
            print(f"    @ f={f}: {v}")

    final_comps = clip.GetFusionCompNameList() or []
    print(f"  final comps on clip: {final_comps}")
    print()
    print("=" * 60)
    print("Now go to the EDIT page, scrub/play clip #1.")
    print("Expected: image rotates 0->90 degrees over first 24 frames.")
    print("If yes: editing the FIRST/ACTIVE comp via approach X is the")
    print("        canonical pattern.")
    print("If no:  Resolve does NOT render comp 1 as default; we need")
    print("        another way to make a specific comp active.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
