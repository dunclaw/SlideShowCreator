"""Determine the working SetKeyFrames payload format for BezierSpline modifiers.

Run with Resolve open on the slideshow demo timeline. This script:

1. Resets the target V1 clip's comps as much as possible.
2. Creates a fresh comp and a chain of 4 Transform tools (XF_S1..XF_S4),
   each whose Angle input is animated via a DIFFERENT keyframe-setting
   strategy. The strategies are:

     S1: spline[time] = value           (subscript assignment)
     S2: spline.SetKeyFrames({t: v, ...})           (scalar values)
     S3: spline.SetKeyFrames({t: [v], ...})         (single-element list)
     S4: spline.SetKeyFrames({t: {1: v, "Flags": {"Linear": True}}, ...})
                                         (full dict per keyframe)

3. Reads back the Angle on each Transform at frames 0, 6, 12, 18, 24.
4. The strategy whose ``GetInput`` returns DIFFERENT values across frames
   is the one that actually keyframes.

Each strategy animates Angle from 0° at frame 0 to 45° at frame 24, so a
working strategy should report ~22.5° (or thereabouts) at frame 12.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Any, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from slideshow.resolve_bridge import ResolveContext, ResolveNotRunningError


def _try(label, fn):
    print("  -> {0}".format(label), end=" ... ")
    try:
        v = fn()
        print("ok ({0!r})".format(v))
        return v
    except Exception as e:
        print("FAIL: {0}".format(type(e).__name__ + ': ' + str(e)))
        return "<<err>>"


def _tool_names(comp):
    tools = comp.GetToolList(False) or {}
    if not isinstance(tools, dict):
        return []
    out = []
    for k in sorted(tools.keys()):
        try:
            out.append(tools[k].Name)
        except Exception:
            out.append(str(k))
    return out


def _delete_all_comps_best_effort(clip):
    """Resolve refuses to delete the currently-active comp. Keep iterating until stable."""
    seen_failures = set()
    for _ in range(8):
        names = clip.GetFusionCompNameList() or []
        progress = False
        for n in names:
            if n in seen_failures:
                continue
            ok = clip.DeleteFusionCompByName(n)
            if ok:
                progress = True
            else:
                seen_failures.add(n)
        if not progress:
            break
    return clip.GetFusionCompNameList() or []


def _setup_clean_comp(clip, fusion):
    """Make a fresh comp the active one. Returns (comp, comp_name)."""
    before = set(clip.GetFusionCompNameList() or [])
    new_handle = clip.AddFusionComp()
    if new_handle is None:
        raise RuntimeError("AddFusionComp returned None")
    after = set(clip.GetFusionCompNameList() or [])
    added = after - before
    if added:
        name = sorted(added)[0]
    else:
        # AddFusionComp didn't add a list entry; fall back to attrs name.
        attrs = new_handle.GetAttrs() if hasattr(new_handle, "GetAttrs") else {}
        name = attrs.get("COMPS_Name") if isinstance(attrs, dict) else None
        if not name:
            raise RuntimeError("Cannot determine name of new comp")
    clip.LoadFusionCompByName(name)
    comp = fusion.GetCurrentComp()
    return comp, name


def _add_named_xform(comp, name):
    """Add a Transform tool with a sentinel display name."""
    xf = comp.AddTool("Transform")
    if xf is None:
        return None
    try:
        xf.SetAttrs({"TOOLS_Name": name})
    except Exception:
        pass
    return xf


def _strategy_subscript(spline):
    spline[0] = 0.0
    spline[24] = 45.0


def _strategy_setkeyframes_scalar(spline):
    spline.SetKeyFrames({0: 0.0, 24: 45.0})


def _strategy_setkeyframes_list(spline):
    spline.SetKeyFrames({0: [0.0], 24: [45.0]})


def _strategy_setkeyframes_dict(spline):
    spline.SetKeyFrames({
        0: {1: 0.0, "Flags": {"Linear": True}},
        24: {1: 45.0, "Flags": {"Linear": True}},
    })


STRATEGIES = [
    ("S1_subscript",        _strategy_subscript),
    ("S2_setkf_scalar",     _strategy_setkeyframes_scalar),
    ("S3_setkf_list",       _strategy_setkeyframes_list),
    ("S4_setkf_fulldict",   _strategy_setkeyframes_dict),
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--track", type=int, default=1)
    p.add_argument("--index", type=int, default=1)
    p.add_argument("--no-cleanup", action="store_true")
    args = p.parse_args(argv)

    try:
        ctx = ResolveContext()
        resolve = ctx.resolve
    except ResolveNotRunningError as e:
        print("FAIL: {0}".format(e), file=sys.stderr)
        return 2
    fusion = resolve.Fusion()

    timeline = ctx.project.GetCurrentTimeline()
    if timeline is None:
        print("FAIL: no timeline"); return 3
    clips = timeline.GetItemListInTrack("video", args.track) or []
    clip = clips[args.index - 1]
    print("Clip: V{0} #{1}: {2!r}".format(args.track, args.index, clip.GetName()))

    if not args.no_cleanup:
        print("\n[cleanup] Existing comps: {0}".format(clip.GetFusionCompNameList() or []))
        remaining = _delete_all_comps_best_effort(clip)
        print("[cleanup] After best-effort delete: {0}".format(remaining))

    comp, comp_name = _setup_clean_comp(clip, fusion)
    print("\n[setup] Working comp name (per GetFusionCompNameList): {0!r}".format(comp_name))
    print("[setup] Tools BEFORE: {0}".format(_tool_names(comp)))

    comp.Lock()
    try:
        mi = comp.FindTool("MediaIn1")
        mo = comp.FindTool("MediaOut1")
        prev = mi

        # Chain four Transform tools and animate each one's Angle via a different strategy.
        results = []
        for tool_name, strategy_fn in STRATEGIES:
            xf_name = "XF_" + tool_name
            print("\n[{0}] Adding {1}".format(tool_name, xf_name))
            xf = _add_named_xform(comp, xf_name)
            _try("xf.ConnectInput('Input', prev.Output)",
                 lambda x=xf, p=prev: x.ConnectInput("Input", p.Output))
            spline = _try("comp.AddTool('BezierSpline')",
                          lambda: comp.AddTool("BezierSpline"))
            if spline and spline != "<<err>>":
                _try("apply strategy {0}".format(tool_name),
                     lambda s=spline, fn=strategy_fn: fn(s))
                _try("xf.ConnectInput('Angle', spline)",
                     lambda x=xf, s=spline: x.ConnectInput("Angle", s))
            results.append((tool_name, xf))
            prev = xf

        _try("mo.ConnectInput('Input', prev.Output)",
             lambda: mo.ConnectInput("Input", prev.Output))
    finally:
        comp.Unlock()

    print("\n[setup] Tools AFTER: {0}".format(_tool_names(comp)))

    # Read back Angle for each transform tool.
    print("\n========== Read-back of Angle at multiple frames ==========")
    print("(Target: 0.0 at frame 0, 45.0 at frame 24; intermediate values ~ linear)")
    print()
    header = "{0:<22}".format("strategy") + "".join("{0:>10}".format("f={0}".format(f)) for f in (0, 6, 12, 18, 24))
    print(header)
    print("-" * len(header))
    for tool_name, xf in results:
        row = "{0:<22}".format(tool_name)
        for f in (0, 6, 12, 18, 24):
            try:
                v = xf.GetInput("Angle", f)
            except Exception as e:
                v = "<err: {0}>".format(e)
            row += "{0:>10}".format("{0:.3f}".format(v) if isinstance(v, (int, float)) else str(v))
        print(row)

    print()
    print("A strategy that animates correctly will show 0.000 at f=0 and 45.000 at f=24,")
    print("with intermediate values between. Constant or stuck-at-default results mean")
    print("that strategy did not actually keyframe.")
    print()
    print("On the Fusion page (this clip), click each XF_* tool and check whether")
    print("Angle shows an animated input + visible keyframes in the inspector.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
