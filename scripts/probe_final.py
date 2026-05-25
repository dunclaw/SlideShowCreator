r"""Final probe: which combo actually lands edits on the target clip?

We've learned:
  * fusion.GetCurrentComp() returns clip-of-selected-clip's comp, NOT
    necessarily the comp we just LoadFusionCompByName'd.
  * AddFusionComp's returned handle is a stub that doesn't persist edits.
  * SetCurrentTimecode moves the playhead but does NOT change Resolve's
    clip-selection / Fusion-loaded-comp.

Untried approaches:
  X. Use the handle RETURNED BY LoadFusionCompByName(name).
  Y. Use clip.GetFusionCompByName(name).
  Z. resolve.OpenPage("fusion") + SetCurrentTimecode + LoadFusionCompByName
     + fusion.GetCurrentComp(). Theory: opening Fusion page UI forces
     the engine to actually load the comp under the playhead.

We also dump dir(fusion) so we can spot any setter we missed.

Run after restarting Resolve (or any clean state):
  py -3.14 scripts\probe_final.py
"""

from __future__ import annotations

import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from slideshow.resolve_bridge import ResolveContext, ResolveNotRunningError


def _names(clip):
    return clip.GetFusionCompNameList() or []


def _fps(timeline) -> float:
    try:
        return float(timeline.GetSetting("timelineFrameRate"))
    except Exception:
        return 24.0


def _frames_to_tc(frames: int, fps: float) -> str:
    frames = int(max(0, frames))
    ifps = int(round(fps))
    h = frames // (3600 * ifps)
    rem = frames - h * 3600 * ifps
    m = rem // (60 * ifps)
    rem -= m * 60 * ifps
    s = rem // ifps
    f = rem - s * ifps
    return "{0:02d}:{1:02d}:{2:02d}:{3:02d}".format(h, m, s, f)


def _edit_chain(comp, suffix, frames=24, rotate=90.0):
    """Lock + Transform + animated BezierSpline. Returns tool name attempted."""
    tool_name = "XF_{0}".format(suffix)
    if comp is None:
        print("    EDIT FAIL: comp is None"); return tool_name, None
    try:
        comp.Lock()
    except Exception as e:
        print("    EDIT FAIL: comp.Lock() raised {0}".format(e)); return tool_name, None
    try:
        mi = comp.FindTool("MediaIn1")
        mo = comp.FindTool("MediaOut1")
        if mi is None or mo is None:
            print("    EDIT FAIL: mi={0!r} mo={1!r}".format(mi, mo)); return tool_name, None
        xf = comp.AddTool("Transform")
        if xf is None:
            print("    EDIT FAIL: AddTool('Transform') returned None"); return tool_name, None
        try: xf.SetAttrs({"TOOLS_Name": tool_name})
        except Exception: pass
        spline = comp.AddTool("BezierSpline")
        if spline is None:
            print("    EDIT FAIL: AddTool('BezierSpline') returned None"); return tool_name, None
        spline.SetKeyFrames({0: [0.0], frames: [float(rotate)]})
        xf.ConnectInput("Input", mi.Output)
        xf.ConnectInput("Angle", spline)
        mo.ConnectInput("Input", xf.Output)
    finally:
        try: comp.Unlock()
        except Exception: pass
    return tool_name, xf


def _dump_tools(comp, label):
    if comp is None:
        print("    [{0}] (comp is None)".format(label)); return
    tools = comp.GetToolList(False) or {}
    if not isinstance(tools, dict):
        print("    [{0}] tools = {1!r}".format(label, tools)); return
    names = []
    for k in sorted(tools.keys()):
        try: names.append(tools[k].Name)
        except Exception: names.append("?")
    print("    [{0}] tools: {1}".format(label, names))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--track", type=int, default=1)
    p.add_argument("--index", type=int, default=None)
    args = p.parse_args(argv)

    try:
        ctx = ResolveContext()
        resolve = ctx.resolve
    except ResolveNotRunningError as e:
        print("FAIL: {0}".format(e), file=sys.stderr); return 2
    fusion = resolve.Fusion()

    timeline = ctx.project.GetCurrentTimeline()
    if timeline is None: print("FAIL: no timeline"); return 3
    clips = timeline.GetItemListInTrack("video", args.track) or []
    index = args.index if args.index is not None else len(clips)
    clip = clips[index - 1]
    print("Target clip: V{0} #{1} of {2}: {3!r}".format(args.track, index, len(clips), clip.GetName()))
    print("Existing comps: {0}".format(_names(clip)))

    # ===== dump candidate setters on the fusion / resolve objects =====
    print("\n--- candidate setters on fusion object ---")
    for a in dir(fusion):
        la = a.lower()
        if any(k in la for k in ("setcur", "loadcomp", "loadcurrent", "opencomp", "activate", "setactive", "switchcomp", "selectcomp")):
            print("  fusion.{0}".format(a))
    print("--- candidate setters on resolve object ---")
    for a in dir(resolve):
        la = a.lower()
        if any(k in la for k in ("setcur", "select", "active", "openclip", "loadclip")):
            print("  resolve.{0}".format(a))
    print("--- candidate setters on timeline object ---")
    for a in dir(timeline):
        la = a.lower()
        if any(k in la for k in ("setcur", "select", "active")):
            print("  timeline.{0}".format(a))

    # ============================================================
    # X: edit through LoadFusionCompByName's RETURN VALUE.
    # ============================================================
    print("\n=== X: edit through LoadFusionCompByName's return value ===")
    before = set(_names(clip)); clip.AddFusionComp(); after = set(_names(clip))
    name_x = sorted(after - before)[0]
    print("  added: {0!r}".format(name_x))
    handle_x = clip.LoadFusionCompByName(name_x)
    print("  LoadFusionCompByName -> {0!r}".format(handle_x))
    tool_name, _ = _edit_chain(handle_x, "X")
    _dump_tools(handle_x, "after edit via X handle")

    # ============================================================
    # Y: edit through GetFusionCompByName(name).
    # ============================================================
    print("\n=== Y: edit through GetFusionCompByName(name) ===")
    before = set(_names(clip)); clip.AddFusionComp(); after = set(_names(clip))
    name_y = sorted(after - before)[0]
    print("  added: {0!r}".format(name_y))
    handle_y = clip.GetFusionCompByName(name_y)
    print("  GetFusionCompByName -> {0!r}".format(handle_y))
    tool_name, _ = _edit_chain(handle_y, "Y")
    _dump_tools(handle_y, "after edit via Y handle")

    # ============================================================
    # Z: OpenPage('fusion') + playhead-into-clip + Load + GetCurrentComp.
    # ============================================================
    print("\n=== Z: OpenPage('fusion') + playhead + LoadByName + GetCurrentComp ===")
    fps = _fps(timeline)
    target_frame = clip.GetStart() + max(1, min(clip.GetDuration() // 2, 5))
    tc = _frames_to_tc(target_frame, fps)
    try:
        opened = resolve.OpenPage("fusion")
        print("  resolve.OpenPage('fusion') -> {0!r}".format(opened))
    except Exception as e:
        print("  OpenPage failed: {0}".format(e))
    try:
        ok = timeline.SetCurrentTimecode(tc)
        print("  SetCurrentTimecode({0!r}) -> {1!r}".format(tc, ok))
    except Exception as e:
        print("  SetCurrentTimecode failed: {0}".format(e))
    # Give Resolve a moment to register the new playhead and auto-load.
    import time
    time.sleep(0.5)
    before = set(_names(clip)); clip.AddFusionComp(); after = set(_names(clip))
    name_z = sorted(after - before)[0]
    print("  added: {0!r}".format(name_z))
    loaded_z = clip.LoadFusionCompByName(name_z)
    print("  LoadFusionCompByName -> {0!r}".format(loaded_z))
    time.sleep(0.3)
    cur_z = fusion.GetCurrentComp()
    print("  fusion.GetCurrentComp() -> {0!r}".format(cur_z))
    _dump_tools(cur_z, "BEFORE edit via Z (GetCurrentComp)")
    tool_name, _ = _edit_chain(cur_z, "Z")
    _dump_tools(cur_z, "AFTER edit via Z handle (GetCurrentComp)")

    print("\n[final] comps on clip: {0}".format(_names(clip)))
    print("\n============================================================")
    print("On the Fusion page of THIS clip (V{0} #{1}, {2!r}):".format(
        args.track, index, clip.GetName()))
    print("  The comp dropdown should list: {0}".format(_names(clip)))
    print("  Switch through each comp; report which contain XF_X / XF_Y / XF_Z.")
    print()
    print("ALSO check whatever clip was selected when you ran the script.")
    print("If XF_Z shows up there, Z still leaked to the wrong clip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
