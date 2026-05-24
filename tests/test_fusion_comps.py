"""Unit tests for slideshow.fusion_comps.

All Fusion / Resolve objects are mocked. These tests verify that we make the
correct API calls — not that Fusion actually applies the animation (that's
what scripts/demo_fusion_animation.py does live).
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from slideshow import fusion_comps as fc


# --------------------------------------------------------------------------- #
# locked()
# --------------------------------------------------------------------------- #

def test_locked_brackets_lock_and_unlock():
    comp = MagicMock()
    with fc.locked(comp) as c:
        assert c is comp
        comp.Lock.assert_called_once()
        comp.Unlock.assert_not_called()
    comp.Unlock.assert_called_once()


def test_locked_unlocks_on_exception():
    comp = MagicMock()
    with pytest.raises(RuntimeError):
        with fc.locked(comp):
            raise RuntimeError("boom")
    comp.Lock.assert_called_once()
    comp.Unlock.assert_called_once()


# --------------------------------------------------------------------------- #
# attach_or_get_comp / remove_comp
# --------------------------------------------------------------------------- #

def test_attach_or_get_comp_creates_when_missing():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = []
    new_comp = MagicMock()
    new_comp.GetAttrs.return_value = "Composition 1"
    ti.AddFusionComp.return_value = new_comp

    result = fc.attach_or_get_comp(ti, "SlideShowCreator")

    assert result is new_comp
    ti.AddFusionComp.assert_called_once()
    ti.RenameFusionCompByName.assert_called_once_with(
        "Composition 1", "SlideShowCreator"
    )


def test_attach_or_get_comp_reuses_existing():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["SlideShowCreator", "other"]
    existing = MagicMock()
    ti.GetFusionCompByName.return_value = existing

    result = fc.attach_or_get_comp(ti, "SlideShowCreator")

    assert result is existing
    ti.AddFusionComp.assert_not_called()
    ti.GetFusionCompByName.assert_called_once_with("SlideShowCreator")


def test_attach_or_get_comp_raises_when_add_returns_none():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = []
    ti.AddFusionComp.return_value = None
    ti.GetName.return_value = "Clip 1"

    with pytest.raises(RuntimeError, match="AddFusionComp"):
        fc.attach_or_get_comp(ti)


def test_attach_or_get_comp_skips_rename_when_already_named():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = []
    new_comp = MagicMock()
    new_comp.GetAttrs.return_value = "SlideShowCreator"
    ti.AddFusionComp.return_value = new_comp

    fc.attach_or_get_comp(ti, "SlideShowCreator")

    ti.RenameFusionCompByName.assert_not_called()


def test_remove_comp_returns_false_when_absent():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["other"]
    assert fc.remove_comp(ti, "SlideShowCreator") is False
    ti.DeleteFusionCompByName.assert_not_called()


def test_remove_comp_deletes_when_present():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["SlideShowCreator"]
    ti.DeleteFusionCompByName.return_value = True
    assert fc.remove_comp(ti, "SlideShowCreator") is True
    ti.DeleteFusionCompByName.assert_called_once_with("SlideShowCreator")


# --------------------------------------------------------------------------- #
# find_tool / find_or_add_tool
# --------------------------------------------------------------------------- #

def test_find_tool_proxies_to_find_tool():
    comp = MagicMock()
    tool = MagicMock()
    comp.FindTool.return_value = tool

    assert fc.find_tool(comp, "MediaIn1") is tool
    comp.FindTool.assert_called_once_with("MediaIn1")


def test_find_or_add_tool_returns_existing():
    comp = MagicMock()
    existing = MagicMock()
    comp.FindTool.return_value = existing

    result = fc.find_or_add_tool(comp, "Transform", "MyXf")

    assert result is existing
    comp.AddTool.assert_not_called()


def test_find_or_add_tool_creates_and_names_when_missing():
    comp = MagicMock()
    comp.FindTool.return_value = None
    new_tool = MagicMock()
    comp.AddTool.return_value = new_tool

    result = fc.find_or_add_tool(comp, "Transform", "MyXf", position=(2, 3))

    assert result is new_tool
    comp.AddTool.assert_called_once_with("Transform", 2, 3)
    new_tool.SetAttrs.assert_called_once_with({"TOOLS_Name": "MyXf"})


def test_find_or_add_tool_tolerates_setattrs_failure():
    comp = MagicMock()
    comp.FindTool.return_value = None
    new_tool = MagicMock()
    new_tool.SetAttrs.side_effect = Exception("not supported in this build")
    comp.AddTool.return_value = new_tool

    # Should not raise — the rename failure is informational, not fatal.
    result = fc.find_or_add_tool(comp, "Transform", "MyXf")
    assert result is new_tool


def test_find_or_add_tool_raises_when_addtool_returns_none():
    comp = MagicMock()
    comp.FindTool.return_value = None
    comp.AddTool.return_value = None

    with pytest.raises(RuntimeError, match="AddTool"):
        fc.find_or_add_tool(comp, "Transform", "MyXf")


# --------------------------------------------------------------------------- #
# connect
# --------------------------------------------------------------------------- #

def test_connect_wires_default_input():
    src = MagicMock()
    src.Output = "src-output-handle"
    dst = MagicMock()

    fc.connect(src, dst)

    dst.ConnectInput.assert_called_once_with("Input", "src-output-handle")


def test_connect_wires_named_input():
    src = MagicMock()
    src.Output = "out"
    dst = MagicMock()

    fc.connect(src, dst, "Background")

    dst.ConnectInput.assert_called_once_with("Background", "out")


def test_connect_falls_back_to_getoutput_when_no_attr():
    src = MagicMock(spec=["GetOutput"])
    src.GetOutput.return_value = "fallback-out"
    dst = MagicMock()

    fc.connect(src, dst)

    src.GetOutput.assert_called_once_with("Output")
    dst.ConnectInput.assert_called_once_with("Input", "fallback-out")


def test_connect_raises_when_source_has_no_output():
    src = MagicMock(spec=[])  # no Output attr, no GetOutput method
    dst = MagicMock()

    with pytest.raises(RuntimeError, match="no Output"):
        fc.connect(src, dst)


# --------------------------------------------------------------------------- #
# keyframe application
# --------------------------------------------------------------------------- #

def test_set_scalar_keyframes_sorts_by_time_and_sets_each():
    tool = MagicMock()
    fc.set_scalar_keyframes(
        tool,
        "Size",
        [(30, 1.0), (0, 0.0), (15, 0.5)],
    )
    assert tool.SetInput.call_args_list == [
        call("Size", 0.0, 0),
        call("Size", 0.5, 15),
        call("Size", 1.0, 30),
    ]


def test_set_scalar_keyframes_empty_does_nothing():
    tool = MagicMock()
    fc.set_scalar_keyframes(tool, "Size", [])
    tool.SetInput.assert_not_called()


def test_set_point_keyframes_passes_tuple_values():
    tool = MagicMock()
    fc.set_point_keyframes(
        tool,
        "Center",
        [(0, (0.0, 0.5)), (24, (0.5, 0.5))],
    )
    assert tool.SetInput.call_args_list == [
        call("Center", (0.0, 0.5), 0),
        call("Center", (0.5, 0.5), 24),
    ]


def test_set_constant_calls_setinput_without_time():
    tool = MagicMock()
    fc.set_constant(tool, "Size", 0.75)
    tool.SetInput.assert_called_once_with("Size", 0.75)


# --------------------------------------------------------------------------- #
# insert_transform_chain
# --------------------------------------------------------------------------- #

def _comp_with_media_io():
    comp = MagicMock()
    media_in = MagicMock(name="MediaIn1")
    media_in.Output = "media-in-out"
    media_out = MagicMock(name="MediaOut1")
    xform = MagicMock(name="Transform")
    xform.Output = "xf-out"

    tools = {"MediaIn1": media_in, "MediaOut1": media_out}

    def find(name):
        return tools.get(name)

    comp.FindTool.side_effect = find
    return comp, media_in, media_out, xform


def test_insert_transform_chain_creates_and_wires_transform():
    comp, media_in, media_out, xform = _comp_with_media_io()
    # FindTool returns None for SlideShowXf the first time, then xform after add.
    seq = [None, None, None, xform]  # MediaIn, MediaOut, find_or_add lookup, then
    # Set up a controlled side_effect that mixes our media-io map and the xform path.
    tools = {"MediaIn1": media_in, "MediaOut1": media_out, "SlideShowXf": None}
    comp.FindTool.side_effect = lambda name: tools.get(name)
    comp.AddTool.return_value = xform

    result = fc.insert_transform_chain(comp)

    assert result is xform
    comp.AddTool.assert_called_once_with("Transform", 1, 0)
    # Verify the wiring: MediaIn -> xform, xform -> MediaOut
    xform.ConnectInput.assert_called_once_with("Input", "media-in-out")
    media_out.ConnectInput.assert_called_once_with("Input", "xf-out")


def test_insert_transform_chain_reuses_existing_transform():
    comp, media_in, media_out, xform = _comp_with_media_io()
    tools = {"MediaIn1": media_in, "MediaOut1": media_out, "SlideShowXf": xform}
    comp.FindTool.side_effect = lambda name: tools.get(name)

    result = fc.insert_transform_chain(comp)

    assert result is xform
    comp.AddTool.assert_not_called()
    # Still rewires (idempotency: safe to call repeatedly)
    xform.ConnectInput.assert_called_once_with("Input", "media-in-out")
    media_out.ConnectInput.assert_called_once_with("Input", "xf-out")


def test_insert_transform_chain_raises_when_media_in_missing():
    comp = MagicMock()
    comp.FindTool.side_effect = lambda name: None

    with pytest.raises(RuntimeError, match="MediaIn1"):
        fc.insert_transform_chain(comp)


def test_insert_transform_chain_raises_when_media_out_missing():
    comp = MagicMock()
    media_in = MagicMock()
    media_in.Output = "out"
    tools = {"MediaIn1": media_in, "MediaOut1": None}
    comp.FindTool.side_effect = lambda name: tools.get(name)

    with pytest.raises(RuntimeError, match="MediaOut1"):
        fc.insert_transform_chain(comp)


# --------------------------------------------------------------------------- #
# TransformAnimation
# --------------------------------------------------------------------------- #

def test_transform_animation_is_empty_true_when_no_fields():
    assert fc.TransformAnimation().is_empty() is True


def test_transform_animation_is_empty_false_with_any_field():
    assert fc.TransformAnimation(size=[(0, 1.0)]).is_empty() is False
    assert fc.TransformAnimation(angle=[(0, 0.0)]).is_empty() is False
    assert fc.TransformAnimation(center=[(0, (0.5, 0.5))]).is_empty() is False
    assert fc.TransformAnimation(pivot=[(0, (0.5, 0.5))]).is_empty() is False


def test_transform_animation_normalizes_sequences_to_lists():
    anim = fc.TransformAnimation(size=((0, 0.0), (10, 1.0)))
    assert isinstance(anim.size, list)
    assert anim.size == [(0, 0.0), (10, 1.0)]


def test_apply_transform_animation_sets_all_provided_inputs():
    tool = MagicMock()
    anim = fc.TransformAnimation(
        center=[(0, (0.0, 0.5)), (12, (0.5, 0.5))],
        size=[(0, 0.5), (12, 1.0)],
        angle=[(0, -10.0), (12, 0.0)],
        pivot=[(0, (0.5, 0.5))],
    )

    fc.apply_transform_animation(tool, anim)

    # Center should be set with point tuples (two keyframes)
    call_list = tool.SetInput.call_args_list
    assert call("Center", (0.0, 0.5), 0) in call_list
    assert call("Center", (0.5, 0.5), 12) in call_list
    assert call("Size", 0.5, 0) in call_list
    assert call("Size", 1.0, 12) in call_list
    assert call("Angle", -10.0, 0) in call_list
    assert call("Angle", 0.0, 12) in call_list
    assert call("Pivot", (0.5, 0.5), 0) in call_list


def test_apply_transform_animation_skips_unset_fields():
    tool = MagicMock()
    anim = fc.TransformAnimation(size=[(0, 1.0)])

    fc.apply_transform_animation(tool, anim)

    # Only Size touched; no Center / Angle / Pivot SetInput calls.
    for c in tool.SetInput.call_args_list:
        args, _kwargs = c
        assert args[0] == "Size", "Unexpected input touched: {0}".format(args[0])


# --------------------------------------------------------------------------- #
# attach_transform_animation (end-to-end with mocks)
# --------------------------------------------------------------------------- #

def test_attach_transform_animation_wires_everything_inside_lock():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = []
    comp = MagicMock()
    comp.GetAttrs.return_value = "SlideShowCreator"  # already correctly named
    ti.AddFusionComp.return_value = comp

    media_in = MagicMock(); media_in.Output = "mi-out"
    media_out = MagicMock()
    xform = MagicMock(); xform.Output = "xf-out"
    tools = {"MediaIn1": media_in, "MediaOut1": media_out, "SlideShowXf": None}

    def find(name):
        return tools.get(name)

    comp.FindTool.side_effect = find
    comp.AddTool.return_value = xform

    # Track lock/unlock order so we can verify edits happen inside the bracket.
    events = []
    comp.Lock.side_effect = lambda: events.append("lock")
    comp.Unlock.side_effect = lambda: events.append("unlock")
    comp.AddTool.side_effect = lambda *a, **kw: (
        events.append("addtool"), xform)[1]
    xform.ConnectInput.side_effect = lambda *a, **kw: events.append("connect-xf")
    media_out.ConnectInput.side_effect = lambda *a, **kw: events.append(
        "connect-out"
    )
    xform.SetInput.side_effect = lambda *a, **kw: events.append("setinput")

    anim = fc.TransformAnimation(size=[(0, 0.0), (24, 1.0)])
    result = fc.attach_transform_animation(ti, anim)

    assert result is xform
    assert events[0] == "lock"
    assert events[-1] == "unlock"
    assert "addtool" in events
    assert "connect-xf" in events
    assert "connect-out" in events
    assert events.count("setinput") == 2


# --------------------------------------------------------------------------- #
# list_tools
# --------------------------------------------------------------------------- #

def test_list_tools_returns_sorted_names():
    comp = MagicMock()
    t1 = MagicMock(); t1.Name = "MediaIn1"
    t2 = MagicMock(); t2.Name = "MediaOut1"
    t3 = MagicMock(); t3.Name = "SlideShowXf"
    comp.GetToolList.return_value = {1: t1, 2: t3, 3: t2}

    names = fc.list_tools(comp)

    assert names == ["MediaIn1", "SlideShowXf", "MediaOut1"]


def test_list_tools_handles_empty():
    comp = MagicMock()
    comp.GetToolList.return_value = None
    assert fc.list_tools(comp) == []
