"""Run focused checks against a live DaVinci Resolve instance.

These checks may edit Fusion compositions or add compositions to timeline clips.
The ``integration`` command is read-only.

Usage:
    py -3.14 scripts\\live_check.py integration
    py -3.14 scripts\\live_check.py active
    py -3.14 scripts\\live_check.py keyframes --track 1 --index 1
    py -3.14 scripts\\live_check.py point-v3
    py -3.14 scripts\\live_check.py target
    py -3.14 scripts\\live_check.py modified
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Callable, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(SCRIPT_DIR), "src"))

from slideshow import fusion_comps as fc  # noqa: E402
from slideshow.resolve_bridge import ResolveContext, ResolveNotRunningError  # noqa: E402


def _context() -> ResolveContext:
    context = ResolveContext()
    resolve = context.resolve
    print("Connected: {0} {1}".format(
        resolve.GetProductName(), resolve.GetVersionString()
    ))
    return context


def _timeline(context: ResolveContext) -> Any:
    timeline = context.current_timeline
    if timeline is None:
        raise RuntimeError("No current timeline. Open a project timeline in Resolve.")
    return timeline


def _target_clip(context: ResolveContext, track: int, index: int) -> Any:
    clips = _timeline(context).GetItemListInTrack("video", track) or []
    if index < 1 or index > len(clips):
        raise RuntimeError(
            "V{0} has {1} clip(s); requested clip #{2}.".format(
                track, len(clips), index
            )
        )
    clip = clips[index - 1]
    print("Target: V{0} #{1}: {2!r}".format(track, index, clip.GetName()))
    return clip


def integration_check(_args: argparse.Namespace) -> int:
    """Verify the running Resolve connection and current project/timeline."""
    context = _context()
    project = context.project
    if project is None:
        raise RuntimeError("Resolve is running, but no project is open.")
    print("Project: {0!r}".format(project.GetName()))

    timeline = _timeline(context)
    print("Timeline: {0!r}".format(timeline.GetName()))
    for track_type in ("video", "audio"):
        count = timeline.GetTrackCount(track_type)
        print("{0} tracks: {1}".format(track_type.capitalize(), count))
        for track in range(1, count + 1):
            items = timeline.GetItemListInTrack(track_type, track) or []
            print("  {0} {1}: {2} item(s)".format(
                track_type.capitalize(), track, len(items)
            ))
    print("PASS: Resolve scripting API, project, and timeline are accessible.")
    return 0


def active_check(args: argparse.Namespace) -> int:
    """Edit the first Fusion comp on one clip, creating it only if needed."""
    context = _context()
    clip = _target_clip(context, args.track, args.index)
    comp = fc.get_active_comp(clip)
    print("Using active comp: {0}".format(comp))

    comp.Lock()
    try:
        transform = comp.FindTool("SlideShowXf")
        if transform is None:
            transform = comp.AddTool("Transform")
            if transform is None:
                raise RuntimeError("AddTool('Transform') returned None.")
            transform.SetAttrs({"TOOLS_Name": "SlideShowXf"})
            media_in = comp.FindTool("MediaIn1")
            media_out = comp.FindTool("MediaOut1")
            if media_in is not None:
                transform.ConnectInput("Input", media_in.Output)
            if media_out is not None:
                media_out.ConnectInput("Input", transform.Output)
            spline = comp.AddTool("BezierSpline")
            if spline is None:
                raise RuntimeError("AddTool('BezierSpline') returned None.")
            spline.SetAttrs({"TOOLS_Name": "SlideShowXfAngle"})
            transform.ConnectInput("Angle", spline)
        else:
            spline = comp.FindTool("SlideShowXfAngle")
            if spline is None:
                spline = comp.AddTool("BezierSpline")
                if spline is None:
                    raise RuntimeError("AddTool('BezierSpline') returned None.")
                spline.SetAttrs({"TOOLS_Name": "SlideShowXfAngle"})
                transform.ConnectInput("Angle", spline)
        spline.SetKeyFrames({0: [0.0], 24: [90.0]})
    finally:
        comp.Unlock()
    fc.mark_modified(comp)
    _read_angle(transform)
    return 0


def _read_angle(transform: Any) -> None:
    for frame in (0, 6, 12, 18, 24):
        try:
            value = transform.GetInput("Angle", frame)
        except Exception as exc:
            value = "<error: {0}>".format(exc)
        print("  Angle at frame {0}: {1}".format(frame, value))


def keyframes_check(args: argparse.Namespace) -> int:
    """Compare four BezierSpline keyframe payload forms on one clip."""
    context = _context()
    clip = _target_clip(context, args.track, args.index)
    comp = fc.get_active_comp(clip)
    strategies: List[Tuple[str, Callable[[Any], None]]] = [
        ("subscript", lambda spline: (
            spline.__setitem__(0, 0.0), spline.__setitem__(24, 45.0)
        )),
        ("scalar", lambda spline: spline.SetKeyFrames({0: 0.0, 24: 45.0})),
        ("list", lambda spline: spline.SetKeyFrames({0: [0.0], 24: [45.0]})),
        ("full-dict", lambda spline: spline.SetKeyFrames({
            0: {1: 0.0, "Flags": {"Linear": True}},
            24: {1: 45.0, "Flags": {"Linear": True}},
        })),
    ]
    results = []
    comp.Lock()
    try:
        for label, apply_strategy in strategies:
            transform = comp.AddTool("Transform")
            spline = comp.AddTool("BezierSpline")
            if transform is None or spline is None:
                raise RuntimeError("Resolve failed to create a Transform or BezierSpline.")
            name = "LiveCheck_{0}".format(label.replace("-", "_"))
            transform.SetAttrs({"TOOLS_Name": name})
            transform.ConnectInput("Angle", spline)
            try:
                apply_strategy(spline)
                print("{0}: keyframes applied".format(label))
            except Exception as exc:
                print("{0}: failed: {1}: {2}".format(
                    label, type(exc).__name__, exc
                ))
            results.append((label, transform))
    finally:
        comp.Unlock()

    print("Read-back (working strategy should approach 0 / 22.5 / 45):")
    for label, transform in results:
        values = []
        for frame in (0, 12, 24):
            try:
                values.append(transform.GetInput("Angle", frame))
            except Exception as exc:
                values.append("<error: {0}>".format(exc))
        print("  {0}: {1}".format(label, values))
    fc.mark_modified(comp)
    return 0


def point_v3_check(_args: argparse.Namespace) -> int:
    """Compare Python and Lua approaches for animating Transform.Center."""
    context = _context()
    timeline = _timeline(context)
    clips = timeline.GetItemListInTrack("video", 1) or []
    if len(clips) < 4:
        raise RuntimeError("point-v3 requires at least 4 clips on V1.")

    comp = fc.get_active_comp(clips[0])
    with fc.locked(comp):
        transform = comp.AddTool("Transform")
        if transform is None:
            raise RuntimeError("AddTool('Transform') returned None.")
        transform.SetAttrs({"TOOLS_Name": "LiveCheck_PointV3"})
        add_modifier = getattr(transform, "AddModifier", None)
        if add_modifier is None:
            print("Transform.AddModifier is unavailable.")
        else:
            try:
                result = add_modifier("Center", "XYPath")
                print("AddModifier('Center', 'XYPath'): {0!r}".format(result))
            except Exception as exc:
                print("AddModifier failed: {0}: {1}".format(
                    type(exc).__name__, exc
                ))
        center = getattr(transform, "Center", None)
        path_tool = _connected_tool(center)
        print("Center connected tool: {0!r}".format(path_tool))
        if path_tool is not None:
            for axis in ("X", "Y"):
                axis_input = getattr(path_tool, axis, None)
                print("{0} connected tool: {1!r}".format(
                    axis, _connected_tool(axis_input)
                ))
    fc.mark_modified(comp)

    lua_cases = (
        ("L1", "LiveCheck_L1", """
local xform = composition:FindTool("LiveCheck_L1")
if xform then
    composition:Lock()
    local ok, err = pcall(function()
        xform:AddModifier("Center", "XYPath")
        xform.Center[0] = {-0.5, 0.5}
        xform.Center[24] = {0.5, 0.5}
    end)
    composition:Unlock()
    if not ok then print("[L1] FAIL: "..tostring(err)) end
else
    print("[L1] Transform not found")
end
"""),
        ("L2", "LiveCheck_L2", """
local xform = composition:FindTool("LiveCheck_L2")
if xform then
    composition:Lock()
    local ok, err = pcall(function()
        xform:AddModifier("Center", "XYPath")
        local path = xform.Center:GetConnectedOutput():GetTool()
        path:AddModifier("X", "BezierSpline")
        path:AddModifier("Y", "BezierSpline")
        local xs = path.X:GetConnectedOutput():GetTool()
        local ys = path.Y:GetConnectedOutput():GetTool()
        xs:SetKeyFrames({[0]={-0.5}, [24]={0.5}}, true)
        ys:SetKeyFrames({[0]={0.5}, [24]={0.5}}, true)
    end)
    composition:Unlock()
    if not ok then print("[L2] FAIL: "..tostring(err)) end
else
    print("[L2] Transform not found")
end
"""),
        ("L3", "LiveCheck_L3", """
print("[L3] Lua Execute reached the composition")
local xform = composition:FindTool("LiveCheck_L3")
if xform then
    composition:Lock()
    xform:SetInput("Center", {0.25, 0.75})
    composition:Unlock()
end
"""),
    )
    for index, (label, tool_name, lua) in enumerate(lua_cases, start=1):
        comp = fc.get_active_comp(clips[index])
        transform = comp.AddTool("Transform")
        if transform is None:
            print("{0}: AddTool('Transform') returned None".format(label))
            continue
        transform.SetAttrs({"TOOLS_Name": tool_name})
        execute = getattr(comp, "Execute", None)
        if execute is None:
            print("{0}: comp.Execute is unavailable".format(label))
        else:
            _try_call("{0} comp.Execute".format(label), execute, lua)
        fc.mark_modified(comp)
        _try_call("{0} Center at frame 0".format(label), transform.GetInput, "Center", 0)
        _try_call("{0} Center at frame 24".format(label), transform.GetInput, "Center", 24)
    return 0


def _connected_tool(input_handle: Any) -> Any:
    if input_handle is None:
        return None
    try:
        output = input_handle.GetConnectedOutput()
        return output.GetTool() if output is not None else None
    except Exception as exc:
        print("Could not inspect connected tool: {0}: {1}".format(
            type(exc).__name__, exc
        ))
        return None


def _edit_chain(comp: Any, suffix: str) -> Optional[str]:
    """Add a visible animated transform to a comp and return its tool name."""
    if comp is None:
        print("  No comp handle.")
        return None
    comp.Lock()
    try:
        media_in = comp.FindTool("MediaIn1")
        media_out = comp.FindTool("MediaOut1")
        if media_in is None or media_out is None:
            print("  Comp is missing MediaIn1 or MediaOut1.")
            return None
        transform = comp.AddTool("Transform")
        spline = comp.AddTool("BezierSpline")
        if transform is None or spline is None:
            print("  Resolve failed to create Transform or BezierSpline.")
            return None
        name = "LiveCheck_" + suffix
        transform.SetAttrs({"TOOLS_Name": name})
        spline.SetKeyFrames({0: [0.0], 24: [90.0]})
        transform.ConnectInput("Input", media_in.Output)
        transform.ConnectInput("Angle", spline)
        media_out.ConnectInput("Input", transform.Output)
        return name
    finally:
        comp.Unlock()


def target_check(_args: argparse.Namespace) -> int:
    """Compare comp handles returned by the three observed loading routes."""
    context = _context()
    timeline = _timeline(context)
    clips = timeline.GetItemListInTrack("video", 1) or []
    if not clips:
        raise RuntimeError("V1 has no clips.")
    clip = clips[-1]
    print("Target: {0!r}".format(clip.GetName()))

    for label, getter in (
        ("load-return", clip.LoadFusionCompByName),
        ("get-by-name", clip.GetFusionCompByName),
    ):
        before = set(clip.GetFusionCompNameList() or [])
        clip.AddFusionComp()
        added = set(clip.GetFusionCompNameList() or []) - before
        if not added:
            raise RuntimeError("AddFusionComp did not add a named composition.")
        name = sorted(added)[0]
        comp = getter(name)
        print("{0}({1!r}) -> {2!r}".format(label, name, comp))
        _edit_chain(comp, label)

    resolve = context.resolve
    try:
        fps = float(timeline.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        fps = 24.0
        print("Could not read timeline frame rate; using 24 fps.")
    frame = clip.GetStart() + max(1, min(clip.GetDuration() // 2, 5))
    tc = _frames_to_timecode(frame, fps)
    _try_call("OpenPage('fusion')", resolve.OpenPage, "fusion")
    _try_call("SetCurrentTimecode", timeline.SetCurrentTimecode, tc)
    time.sleep(0.5)
    before = set(clip.GetFusionCompNameList() or [])
    clip.AddFusionComp()
    added = set(clip.GetFusionCompNameList() or []) - before
    if not added:
        raise RuntimeError("AddFusionComp did not add a named composition.")
    name = sorted(added)[0]
    _try_call("LoadFusionCompByName", clip.LoadFusionCompByName, name)
    time.sleep(0.3)
    current_comp = context.fusion.GetCurrentComp()
    print("fusion.GetCurrentComp() -> {0!r}".format(current_comp))
    _edit_chain(current_comp, "current-comp")
    return 0


def _frames_to_timecode(frames: int, fps: float) -> str:
    integer_fps = max(1, int(round(fps)))
    frames = max(0, int(frames))
    hours, remainder = divmod(frames, 3600 * integer_fps)
    minutes, remainder = divmod(remainder, 60 * integer_fps)
    seconds, frame = divmod(remainder, integer_fps)
    return "{0:02d}:{1:02d}:{2:02d}:{3:02d}".format(
        hours, minutes, seconds, frame
    )


def modified_check(_args: argparse.Namespace) -> int:
    """Compare baseline, SetAttrs, Save, and combined calls on separate clips."""
    context = _context()
    timeline = _timeline(context)
    clips = timeline.GetItemListInTrack("video", 1) or []
    if len(clips) < 3:
        raise RuntimeError("Modified check requires at least 3 clips on V1.")

    labels = ["baseline", "setattrs", "save"]
    if len(clips) >= 4:
        labels.append("both")
    for index, label in enumerate(labels):
        clip = clips[index]
        comp = fc.get_active_comp(clip)
        print("V1 #{0} {1}: {2!r}".format(index + 1, label, clip.GetName()))
        comp.Lock()
        try:
            media_in = comp.FindTool("MediaIn1")
            media_out = comp.FindTool("MediaOut1")
            transform = comp.AddTool("Transform")
            spline = comp.AddTool("BezierSpline")
            if media_in is None or media_out is None or transform is None or spline is None:
                raise RuntimeError("Resolve failed to create probe tools.")
            transform.SetAttrs({"TOOLS_Name": "LiveCheck_" + label})
            transform.ConnectInput("Input", media_in.Output)
            transform.ConnectInput("Angle", spline)
            spline.SetKeyFrames({0: [0.0], 24: [90.0]})
            media_out.ConnectInput("Input", transform.Output)
        finally:
            comp.Unlock()
        if label in ("setattrs", "both"):
            _try_call("SetAttrs COMPB_Modified", comp.SetAttrs, {"COMPB_Modified": True})
        if label in ("save", "both"):
            _try_call("Save", comp.Save)
        if label == "baseline":
            print("  No modified/save call (control).")
    print("Compare edit-page playback and the modified marker for each clip.")
    return 0


def _try_call(label: str, function: Callable[..., Any], *args: Any) -> None:
    try:
        print("  {0}: {1!r}".format(label, function(*args)))
    except Exception as exc:
        print("  {0} failed: {1}: {2}".format(
            label, type(exc).__name__, exc
        ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    integration = commands.add_parser(
        "integration", help="read-only Resolve, project, and timeline check"
    )
    integration.set_defaults(run=integration_check)

    active = commands.add_parser("active", help="animate the active comp on one clip")
    active.add_argument("--track", type=int, default=1)
    active.add_argument("--index", type=int, default=1)
    active.set_defaults(run=active_check)

    keyframes = commands.add_parser(
        "keyframes", help="compare BezierSpline keyframe payloads"
    )
    keyframes.add_argument("--track", type=int, default=1)
    keyframes.add_argument("--index", type=int, default=1)
    keyframes.set_defaults(run=keyframes_check)

    point = commands.add_parser(
        "point-v3", help="check Python AddModifier for XYPath"
    )
    point.set_defaults(run=point_v3_check)

    target = commands.add_parser(
        "target", help="compare Fusion composition handles"
    )
    target.set_defaults(run=target_check)

    modified = commands.add_parser(
        "modified", help="compare Fusion composition modified/save calls"
    )
    modified.set_defaults(run=modified_check)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (ResolveNotRunningError, RuntimeError) as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
