"""
probe_point_v3.py — Animate Center via the CANONICAL Fusion idiom:
``tool.AddModifier(inputID, "XYPath")``.

Research findings (see api-notes.md):
- ``comp.AddTool("XYPath")`` + manual ``ConnectInput`` produces an unwired stub.
  The XYPath's ``.Output`` is type "Path", not "Point", so the connection
  silently does nothing.
- The correct call is ``tool.AddModifier(inputID, "XYPath")`` which creates
  the XYPath AND binds it into the Point input in one atomic step.
- After that, in Lua you can subscript-assign ``tool.Center[frame] = {x, y}``.
- The Python IPC proxy does NOT implement ``__setitem__`` on input objects,
  so the subscript assignment must happen in Lua via ``comp.Execute(...)``.

This probe tests 4 candidates in priority order:

CAND P: Python ``xform.AddModifier("Center", "XYPath")`` + navigate to child
        BezierSplines via ``GetConnectedOutput().GetTool()`` + ``SetKeyFrames``.
        Pure Python — preferred if it works because no Lua escape hatch needed.

CAND L1: ``comp.Execute()`` with Lua doing AddModifier + ``Center[frame]={x,y}``
         subscript assignment. The canonical Fusion idiom.

CAND L2: ``comp.Execute()`` with Lua doing AddModifier + navigate to X/Y
         BezierSpline children + ``SetKeyFrames``. More verbose but mirrors
         what AutoSubs and xclipioFusion.lua do.

CAND L3: ``comp.Execute()`` minimal smoke test — just print something to
         verify Execute even runs in our LoadFusionCompByName-handle context.

USER MUST: have at least 4 clips on V1, each with just a single empty
'Composition 1'.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from slideshow.resolve_bridge import ResolveContext
from slideshow import fusion_comps as fc


def try_call(label, fn, *args, **kwargs):
    print(f"    -> {label} ... ", end="")
    try:
        r = fn(*args, **kwargs)
        print(f"ok ({r!r})")
        return r
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return None


def cand_p_python_addmodifier(clip):
    """Pure Python: AddModifier + navigate to X/Y splines + SetKeyFrames."""
    comp = fc.get_active_comp(clip)
    with fc.locked(comp):
        xform = fc.insert_transform_chain(comp)
        xform.SetAttrs({"TOOLS_Name": "XF_P_PYAM"})

        add_mod = getattr(xform, "AddModifier", None)
        print(f"    xform.AddModifier present: {add_mod is not None}")
        if add_mod is not None:
            try_call("xform.AddModifier('Center', 'XYPath')",
                     xform.AddModifier, "Center", "XYPath")

        # Try to navigate from xform.Center -> connected XYPath -> X/Y children.
        center_inp = getattr(xform, "Center", None)
        print(f"    xform.Center: {center_inp}")
        xypath = None
        if center_inp is not None:
            gco = getattr(center_inp, "GetConnectedOutput", None)
            if gco is not None:
                out = try_call("xform.Center.GetConnectedOutput()", gco)
                if out is not None:
                    get_tool = getattr(out, "GetTool", None)
                    if get_tool is not None:
                        xypath = try_call("Center.GetConnectedOutput().GetTool()",
                                          get_tool)
        print(f"    XYPath via navigation: {xypath}")

        # If navigation worked, try to AddModifier on X and Y inputs.
        if xypath is not None:
            xp_add = getattr(xypath, "AddModifier", None)
            if xp_add is not None:
                try_call("xypath.AddModifier('X','BezierSpline')",
                         xypath.AddModifier, "X", "BezierSpline")
                try_call("xypath.AddModifier('Y','BezierSpline')",
                         xypath.AddModifier, "Y", "BezierSpline")
            x_inp = getattr(xypath, "X", None)
            y_inp = getattr(xypath, "Y", None)
            print(f"    xypath.X: {x_inp} ; xypath.Y: {y_inp}")
            x_spline = None
            y_spline = None
            if x_inp is not None:
                go = getattr(x_inp, "GetConnectedOutput", None)
                if go is not None:
                    o = go()
                    if o is not None:
                        x_spline = o.GetTool()
            if y_inp is not None:
                go = getattr(y_inp, "GetConnectedOutput", None)
                if go is not None:
                    o = go()
                    if o is not None:
                        y_spline = o.GetTool()
            print(f"    X spline: {x_spline} ; Y spline: {y_spline}")
            if x_spline is not None:
                try_call("x_spline.SetKeyFrames {0:[-0.5],24:[0.5]}",
                         x_spline.SetKeyFrames, {0: [-0.5], 24: [0.5]})
            if y_spline is not None:
                try_call("y_spline.SetKeyFrames {0:[0.5],24:[0.5]}",
                         y_spline.SetKeyFrames, {0: [0.5], 24: [0.5]})
    fc.mark_modified(comp)
    return comp


def cand_l1_lua_subscript(clip):
    """comp.Execute Lua: AddModifier + subscript Center[frame] = {x, y}."""
    comp = fc.get_active_comp(clip)
    # Don't lock from Python here — let the Lua do it inside Execute.
    xform = fc.insert_transform_chain(comp)
    xform.SetAttrs({"TOOLS_Name": "XF_L1_LUASUB"})
    fc.mark_modified(comp)

    has_exec = getattr(comp, "Execute", None) is not None
    print(f"    comp.Execute present: {has_exec}")
    if not has_exec:
        print("    SKIP — Execute not available")
        return comp

    lua = r"""
local xform = composition:FindTool("XF_L1_LUASUB")
if xform == nil then
    print("[lua L1] ERROR: XF_L1_LUASUB not found")
    return
end
print("[lua L1] found xform: "..tostring(xform.Name))
composition:Lock()
local ok, err = pcall(function()
    xform:AddModifier("Center", "XYPath")
    print("[lua L1] AddModifier called")
    xform.Center[0]  = {-0.5, 0.5}
    xform.Center[24] = { 0.5, 0.5}
    print("[lua L1] subscripts assigned")
end)
composition:Unlock()
if not ok then print("[lua L1] FAIL: "..tostring(err)) end
print("[lua L1] done")
"""
    try_call("comp.Execute(lua L1)", comp.Execute, lua)
    return comp


def cand_l2_lua_child_splines(clip):
    """comp.Execute Lua: AddModifier + nested AddModifier on X/Y + SetKeyFrames."""
    comp = fc.get_active_comp(clip)
    xform = fc.insert_transform_chain(comp)
    xform.SetAttrs({"TOOLS_Name": "XF_L2_CHILDREN"})
    fc.mark_modified(comp)

    has_exec = getattr(comp, "Execute", None) is not None
    if not has_exec:
        print("    SKIP — Execute not available")
        return comp

    lua = r"""
local xform = composition:FindTool("XF_L2_CHILDREN")
if xform == nil then print("[lua L2] ERROR: not found"); return end
composition:Lock()
local ok, err = pcall(function()
    xform:AddModifier("Center", "XYPath")
    local xyPath = xform.Center:GetConnectedOutput():GetTool()
    if xyPath == nil then
        print("[lua L2] xyPath nil after AddModifier"); return
    end
    print("[lua L2] xyPath: "..tostring(xyPath.Name))
    xyPath:AddModifier("X", "BezierSpline")
    xyPath:AddModifier("Y", "BezierSpline")
    local xs = xyPath.X:GetConnectedOutput():GetTool()
    local ys = xyPath.Y:GetConnectedOutput():GetTool()
    if xs then xs:SetKeyFrames({[0]={-0.5}, [24]={0.5}}, true) end
    if ys then ys:SetKeyFrames({[0]={ 0.5}, [24]={0.5}}, true) end
    print("[lua L2] keyframes set on x="..tostring(xs)..", y="..tostring(ys))
end)
composition:Unlock()
if not ok then print("[lua L2] FAIL: "..tostring(err)) end
print("[lua L2] done")
"""
    try_call("comp.Execute(lua L2)", comp.Execute, lua)
    return comp


def cand_l3_lua_smoke(clip):
    """comp.Execute Lua: minimal smoke test — does Execute even run on
    our LoadFusionCompByName handle?"""
    comp = fc.get_active_comp(clip)
    xform = fc.insert_transform_chain(comp)
    xform.SetAttrs({"TOOLS_Name": "XF_L3_SMOKE"})
    fc.mark_modified(comp)

    has_exec = getattr(comp, "Execute", None) is not None
    print(f"    comp.Execute present: {has_exec}")
    if not has_exec:
        return comp

    lua = r"""
print("[lua L3] hello from Lua")
print("[lua L3] composition: "..tostring(composition))
print("[lua L3] composition.Name: "..tostring(composition.Name))
local xform = composition:FindTool("XF_L3_SMOKE")
print("[lua L3] xform found: "..tostring(xform))
if xform ~= nil then
    print("[lua L3] xform.ID: "..tostring(xform.ID))
    -- Just set a static Center value to confirm we can mutate the comp
    composition:Lock()
    xform:SetInput("Center", {0.25, 0.75})
    composition:Unlock()
    print("[lua L3] static SetInput Center=(0.25,0.75) done")
end
"""
    try_call("comp.Execute(lua L3)", comp.Execute, lua)
    return comp


def read_center(comp, tool_name):
    xf = comp.FindTool(tool_name)
    if xf is None:
        return f"tool {tool_name} not found"
    try:
        v0 = xf.GetInput("Center", 0)
    except Exception as exc:
        v0 = f"ERR:{exc}"
    try:
        v24 = xf.GetInput("Center", 24)
    except Exception as exc:
        v24 = f"ERR:{exc}"
    return f"f=0 -> {v0}  ;  f=24 -> {v24}"


def main() -> int:
    ctx = ResolveContext()
    resolve = ctx.resolve
    timeline = ctx.current_timeline
    print(f"Connected: {resolve.GetProductName()} {resolve.GetVersionString()}")
    items = timeline.GetItemListInTrack("video", 1) or []
    print(f"V1 has {len(items)} clip(s)")
    if len(items) < 4:
        print("Need at least 4 clips on V1. Abort.")
        return 1

    print()
    print("=== CAND P: Python AddModifier + child SetKeyFrames on V1 #1 ===")
    comp_p = cand_p_python_addmodifier(items[0])
    print(f"  tools: {fc.list_tools(comp_p)}")

    print()
    print("=== CAND L1: comp.Execute Lua subscript Center[f]={x,y} on V1 #2 ===")
    comp_l1 = cand_l1_lua_subscript(items[1])
    print(f"  tools: {fc.list_tools(comp_l1)}")

    print()
    print("=== CAND L2: comp.Execute Lua AddModifier+child SetKeyFrames on V1 #3 ===")
    comp_l2 = cand_l2_lua_child_splines(items[2])
    print(f"  tools: {fc.list_tools(comp_l2)}")

    print()
    print("=== CAND L3: comp.Execute Lua smoke test on V1 #4 ===")
    comp_l3 = cand_l3_lua_smoke(items[3])
    print(f"  tools: {fc.list_tools(comp_l3)}")

    print()
    print("=" * 60)
    print("Center read-back per candidate (expect different vals @ f=0 vs f=24,")
    print("EXCEPT L3 which sets a constant {0.25, 0.75} on both frames):")
    print(f"  P  (XF_P_PYAM):     {read_center(comp_p,  'XF_P_PYAM')}")
    print(f"  L1 (XF_L1_LUASUB):  {read_center(comp_l1, 'XF_L1_LUASUB')}")
    print(f"  L2 (XF_L2_CHILDREN):{read_center(comp_l2, 'XF_L2_CHILDREN')}")
    print(f"  L3 (XF_L3_SMOKE):   {read_center(comp_l3, 'XF_L3_SMOKE')}")

    print()
    print("Check the EDIT page: which clips (1-4) show slide-in motion?")
    print("Also check the FUSION page node graph for each — does the Transform")
    print("show an animated/coloured Center input + modifier 'arrow' badge?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
