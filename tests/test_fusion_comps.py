"""Unit tests for slideshow.fusion_comps.

All Fusion / Resolve objects are mocked. These tests verify that we make the
correct API calls — not that Fusion actually applies the animation (that's
what the live probe scripts under ``scripts/`` do).
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
# get_active_comp
# --------------------------------------------------------------------------- #

def test_get_active_comp_uses_first_existing_comp_via_loadbyname():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["Composition 1", "Composition 2"]
    loaded = MagicMock(name="loaded-comp")
    ti.LoadFusionCompByName.return_value = loaded

    result = fc.get_active_comp(ti)

    assert result is loaded
    ti.LoadFusionCompByName.assert_called_once_with("Composition 1")
    ti.AddFusionComp.assert_not_called()
    # We must NOT use GetFusionCompByName — it returns a stub handle.
    ti.GetFusionCompByName.assert_not_called()


def test_get_active_comp_creates_comp_when_clip_has_none():
    ti = MagicMock()
    # First call: no comps; AddFusionComp then list grows.
    ti.GetFusionCompNameList.side_effect = [[], ["Composition 1"]]
    ti.AddFusionComp.return_value = "Composition 1"
    loaded = MagicMock()
    ti.LoadFusionCompByName.return_value = loaded

    result = fc.get_active_comp(ti)

    assert result is loaded
    ti.AddFusionComp.assert_called_once()
    ti.LoadFusionCompByName.assert_called_once_with("Composition 1")


def test_get_active_comp_raises_when_addfusioncomp_fails():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = []
    ti.AddFusionComp.return_value = None
    ti.GetName.return_value = "Clip 1"

    with pytest.raises(RuntimeError, match="AddFusionComp"):
        fc.get_active_comp(ti)


def test_get_active_comp_raises_when_load_returns_none():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["Composition 1"]
    ti.LoadFusionCompByName.return_value = None
    ti.GetName.return_value = "Clip 1"

    with pytest.raises(RuntimeError, match="LoadFusionCompByName"):
        fc.get_active_comp(ti)


def test_attach_or_get_comp_is_alias_of_get_active_comp():
    assert fc.attach_or_get_comp is fc.get_active_comp


# --------------------------------------------------------------------------- #
# mark_modified
# --------------------------------------------------------------------------- #

def test_mark_modified_sets_compb_modified_true():
    comp = MagicMock()
    fc.mark_modified(comp)
    comp.SetAttrs.assert_called_once_with({"COMPB_Modified": True})


def test_mark_modified_swallows_exceptions():
    comp = MagicMock()
    comp.SetAttrs.side_effect = Exception("not supported")
    # Should not raise even if the attr is rejected.
    fc.mark_modified(comp)


# --------------------------------------------------------------------------- #
# find_tool / find_or_add_tool / connect
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
    result = fc.find_or_add_tool(comp, "Transform", "MyXf")
    assert result is new_tool


def test_find_or_add_tool_raises_when_addtool_returns_none():
    comp = MagicMock()
    comp.FindTool.return_value = None
    comp.AddTool.return_value = None
    with pytest.raises(RuntimeError, match="AddTool"):
        fc.find_or_add_tool(comp, "Transform", "MyXf")


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
    src = MagicMock(spec=[])
    dst = MagicMock()
    with pytest.raises(RuntimeError, match="no Output"):
        fc.connect(src, dst)


# --------------------------------------------------------------------------- #
# set_scalar_keyframes (BezierSpline based)
# --------------------------------------------------------------------------- #

def _comp_with(addtool_returns):
    """Build a mocked comp whose AddTool returns the provided sequence."""
    comp = MagicMock()
    comp.FindTool.return_value = None
    if isinstance(addtool_returns, list):
        comp.AddTool.side_effect = addtool_returns
    else:
        comp.AddTool.return_value = addtool_returns
    return comp


def test_set_scalar_keyframes_empty_does_nothing():
    comp = MagicMock()
    tool = MagicMock()
    result = fc.set_scalar_keyframes(comp, tool, "Size", [])
    assert result is None
    comp.AddTool.assert_not_called()
    tool.SetInput.assert_not_called()


def test_set_scalar_keyframes_single_keyframe_sets_constant():
    comp = MagicMock()
    tool = MagicMock()
    result = fc.set_scalar_keyframes(comp, tool, "Size", [(0, 0.75)])
    assert result is None
    comp.AddTool.assert_not_called()
    tool.SetInput.assert_called_once_with("Size", 0.75)


def test_set_scalar_keyframes_two_or_more_builds_spline_and_connects():
    spline = MagicMock()
    comp = _comp_with(spline)
    tool = MagicMock()
    tool.GetAttrs.return_value = "SlideShowXf"

    result = fc.set_scalar_keyframes(comp, tool, "Angle", [(0, 0.0), (24, 90.0)])

    assert result is spline
    comp.AddTool.assert_called_once_with("BezierSpline")
    spline.SetKeyFrames.assert_called_once_with({0: [0.0], 24: [90.0]})
    tool.ConnectInput.assert_called_once_with("Angle", spline)


def test_set_scalar_keyframes_sorts_keyframes_before_setkeyframes():
    spline = MagicMock()
    comp = _comp_with(spline)
    tool = MagicMock()
    tool.GetAttrs.return_value = "X"

    fc.set_scalar_keyframes(comp, tool, "Angle", [(30, 1.0), (0, 0.0), (15, 0.5)])

    # SetKeyFrames should get a dict with keys 0, 15, 30
    args, _kw = spline.SetKeyFrames.call_args
    assert args[0] == {0: [0.0], 15: [0.5], 30: [1.0]}


def test_set_scalar_keyframes_reuses_existing_named_spline():
    existing_spline = MagicMock(name="existing")
    comp = MagicMock()
    tool = MagicMock()
    tool.GetAttrs.return_value = "SlideShowXf"
    # FindTool returns the existing spline for the expected name.
    comp.FindTool.side_effect = lambda name: (
        existing_spline if name == "SlideShowXfAngle" else None
    )

    result = fc.set_scalar_keyframes(
        comp, tool, "Angle", [(0, 0.0), (24, 90.0)]
    )

    assert result is existing_spline
    comp.AddTool.assert_not_called()  # reused, not added
    existing_spline.SetKeyFrames.assert_called_once_with({0: [0.0], 24: [90.0]})
    tool.ConnectInput.assert_called_once_with("Angle", existing_spline)


def test_set_scalar_keyframes_raises_when_addtool_returns_none():
    comp = _comp_with(None)
    tool = MagicMock()
    tool.GetAttrs.return_value = "X"
    with pytest.raises(RuntimeError, match="BezierSpline"):
        fc.set_scalar_keyframes(comp, tool, "Size", [(0, 0.0), (24, 1.0)])


# --------------------------------------------------------------------------- #
# set_point_keyframes (XYPath + child BezierSpline based, via AddModifier)
# --------------------------------------------------------------------------- #


class _FakeInput:
    """Minimal stand-in for a Fusion Input object.

    Real Fusion inputs expose ``GetConnectedOutput()`` which returns an
    ``Output`` object (or ``None``); that Output has ``GetTool()`` returning
    the tool currently feeding the input. We need a mutable mock so a test
    can simulate the side-effect of an ``AddModifier`` call connecting a
    new tool to the input.
    """

    def __init__(self, connected=None):
        self.connected = connected

    def GetConnectedOutput(self):
        if self.connected is None:
            return None
        out = MagicMock(name="connected-output")
        out.GetTool.return_value = self.connected
        return out


def _make_xypath_mock():
    """Build a mock XYPath with X/Y inputs and an AddModifier that wires
    child BezierSplines on demand. Returns (xypath, x_spline, y_spline)."""
    xypath = MagicMock(name="xypath")
    xypath.X = _FakeInput()
    xypath.Y = _FakeInput()
    x_spline = MagicMock(name="x_spline")
    y_spline = MagicMock(name="y_spline")

    def xypath_addmod(input_name, kind):
        assert kind == "BezierSpline"
        if input_name == "X":
            xypath.X.connected = x_spline
        elif input_name == "Y":
            xypath.Y.connected = y_spline
        return True

    xypath.AddModifier.side_effect = xypath_addmod
    return xypath, x_spline, y_spline


def _make_tool_with_point_input(input_name, xypath_to_attach):
    """Build a mock Transform-like tool with the named Point input ready for
    AddModifier to attach the given xypath mock."""
    tool = MagicMock(name="tool")
    setattr(tool, input_name, _FakeInput())

    def tool_addmod(name, kind):
        assert kind == "XYPath"
        if name == input_name:
            getattr(tool, input_name).connected = xypath_to_attach
        return True

    tool.AddModifier.side_effect = tool_addmod
    return tool


def test_set_point_keyframes_empty_does_nothing():
    comp = MagicMock()
    tool = MagicMock()
    result = fc.set_point_keyframes(comp, tool, "Center", [])
    assert result is None
    tool.AddModifier.assert_not_called()


def test_set_point_keyframes_single_sets_constant_as_xy_list():
    comp = MagicMock()
    tool = MagicMock()
    result = fc.set_point_keyframes(comp, tool, "Center", [(0, (0.5, 0.5))])
    assert result is None
    tool.AddModifier.assert_not_called()
    tool.SetInput.assert_called_once_with("Center", [0.5, 0.5])


def test_set_point_keyframes_two_keyframes_addmodifies_xypath_and_children():
    xypath, x_spline, y_spline = _make_xypath_mock()
    tool = _make_tool_with_point_input("Center", xypath)
    comp = MagicMock()

    result = fc.set_point_keyframes(
        comp, tool, "Center", [(0, (-0.5, 0.5)), (24, (0.5, 0.5))]
    )

    assert result is xypath
    tool.AddModifier.assert_called_once_with("Center", "XYPath")
    child_mod_calls = [c.args for c in xypath.AddModifier.call_args_list]
    assert ("X", "BezierSpline") in child_mod_calls
    assert ("Y", "BezierSpline") in child_mod_calls
    x_spline.SetKeyFrames.assert_called_once_with({0: [-0.5], 24: [0.5]})
    y_spline.SetKeyFrames.assert_called_once_with({0: [0.5], 24: [0.5]})


def test_set_point_keyframes_sorts_keyframes_before_setkeyframes():
    xypath, x_spline, y_spline = _make_xypath_mock()
    tool = _make_tool_with_point_input("Center", xypath)
    comp = MagicMock()

    fc.set_point_keyframes(
        comp, tool, "Center",
        [(24, (1.0, 1.0)), (0, (0.0, 0.0)), (12, (0.5, 0.5))],
    )

    x_args = x_spline.SetKeyFrames.call_args.args[0]
    y_args = y_spline.SetKeyFrames.call_args.args[0]
    assert x_args == {0: [0.0], 12: [0.5], 24: [1.0]}
    assert y_args == {0: [0.0], 12: [0.5], 24: [1.0]}


def test_set_point_keyframes_reuses_existing_xypath_modifier():
    """If the input is already wired to an XYPath, no new modifier is added."""
    xypath, x_spline, y_spline = _make_xypath_mock()
    # Pre-wire both layers so _connected_tool finds them immediately.
    xypath.X.connected = x_spline
    xypath.Y.connected = y_spline
    tool = MagicMock(name="tool")
    tool.Center = _FakeInput(connected=xypath)
    comp = MagicMock()

    result = fc.set_point_keyframes(
        comp, tool, "Center", [(0, (-0.5, 0.5)), (24, (0.5, 0.5))]
    )

    assert result is xypath
    tool.AddModifier.assert_not_called()
    xypath.AddModifier.assert_not_called()
    x_spline.SetKeyFrames.assert_called_once_with({0: [-0.5], 24: [0.5]})
    y_spline.SetKeyFrames.assert_called_once_with({0: [0.5], 24: [0.5]})


def test_set_point_keyframes_raises_when_addmodifier_returns_false():
    tool = MagicMock()
    tool.Center = _FakeInput()  # never gets connected
    tool.AddModifier.return_value = False
    comp = MagicMock()
    with pytest.raises(RuntimeError, match="AddModifier"):
        fc.set_point_keyframes(
            comp, tool, "Center", [(0, (-0.5, 0.5)), (24, (0.5, 0.5))]
        )


def test_set_point_keyframes_raises_when_xypath_not_wired_after_addmodifier():
    tool = MagicMock()
    tool.Center = _FakeInput()  # AddModifier returns True but input stays disconnected
    tool.AddModifier.return_value = True
    comp = MagicMock()
    with pytest.raises(RuntimeError, match="XYPath"):
        fc.set_point_keyframes(
            comp, tool, "Center", [(0, (-0.5, 0.5)), (24, (0.5, 0.5))]
        )


# --------------------------------------------------------------------------- #
# set_constant
# --------------------------------------------------------------------------- #

def test_set_constant_calls_setinput_without_time():
    tool = MagicMock()
    fc.set_constant(tool, "Size", 0.75)
    tool.SetInput.assert_called_once_with("Size", 0.75)


# --------------------------------------------------------------------------- #
# insert_transform_chain
# --------------------------------------------------------------------------- #

def _comp_with_media_io(slideshow_xf=None):
    comp = MagicMock()
    media_in = MagicMock(name="MediaIn1")
    media_in.Output = "media-in-out"
    media_out = MagicMock(name="MediaOut1")
    xform = MagicMock(name="Transform")
    xform.Output = "xf-out"
    tools = {
        "MediaIn1": media_in,
        "MediaOut1": media_out,
        "SlideShowXf": slideshow_xf,
    }
    comp.FindTool.side_effect = lambda name: tools.get(name)
    return comp, media_in, media_out, xform


def test_insert_transform_chain_creates_and_wires_transform():
    comp, media_in, media_out, xform = _comp_with_media_io(slideshow_xf=None)
    comp.AddTool.return_value = xform

    result = fc.insert_transform_chain(comp)

    assert result is xform
    comp.AddTool.assert_called_once_with("Transform", 1, 0)
    xform.ConnectInput.assert_called_once_with("Input", "media-in-out")
    media_out.ConnectInput.assert_called_once_with("Input", "xf-out")


def test_insert_transform_chain_reuses_existing_transform():
    comp, media_in, media_out, xform = _comp_with_media_io(slideshow_xf=None)
    # Now make SlideShowXf already exist.
    existing = MagicMock(name="existing-xf")
    existing.Output = "existing-out"
    comp.FindTool.side_effect = lambda name: {
        "MediaIn1": media_in, "MediaOut1": media_out, "SlideShowXf": existing,
    }.get(name)

    result = fc.insert_transform_chain(comp)

    assert result is existing
    comp.AddTool.assert_not_called()
    existing.ConnectInput.assert_called_once_with("Input", "media-in-out")
    media_out.ConnectInput.assert_called_once_with("Input", "existing-out")


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


def test_apply_transform_animation_routes_to_correct_setters():
    """Verify each field is dispatched to the right keyframe setter."""
    comp = MagicMock()
    # Two BezierSplines are added directly (one for Size, one for Angle).
    # Two XYPaths are attached via tool.AddModifier, so they don't appear
    # in comp.AddTool's call list — they appear in tool.AddModifier's.
    fake_spline_size = MagicMock(name="size-spline")
    fake_spline_angle = MagicMock(name="angle-spline")
    comp.AddTool.side_effect = [fake_spline_size, fake_spline_angle]
    comp.FindTool.return_value = None  # no existing modifiers

    # Build a tool mock whose Center/Pivot inputs go through the same
    # AddModifier → child-spline navigation dance that the real Fusion
    # bridge supports.
    xy_center, xs_c, ys_c = _make_xypath_mock()
    xy_pivot, xs_p, ys_p = _make_xypath_mock()
    tool = MagicMock(name="tool")
    tool.Center = _FakeInput()
    tool.Pivot = _FakeInput()
    tool.GetAttrs.return_value = "X"

    def tool_addmod(name, kind):
        assert kind == "XYPath"
        if name == "Center":
            tool.Center.connected = xy_center
        elif name == "Pivot":
            tool.Pivot.connected = xy_pivot
        return True

    tool.AddModifier.side_effect = tool_addmod

    anim = fc.TransformAnimation(
        center=[(0, (0.0, 0.5)), (12, (0.5, 0.5))],
        pivot=[(0, (0.5, 0.5)), (12, (0.6, 0.6))],
        size=[(0, 0.5), (12, 1.0)],
        angle=[(0, -10.0), (12, 0.0)],
    )

    fc.apply_transform_animation(comp, tool, anim)

    # Only BezierSplines are added directly to the comp.
    assert comp.AddTool.call_args_list[0].args == ("BezierSpline",)
    assert comp.AddTool.call_args_list[1].args == ("BezierSpline",)
    # XYPaths are attached to the tool via AddModifier.
    tool_mods = [c.args for c in tool.AddModifier.call_args_list]
    assert ("Center", "XYPath") in tool_mods
    assert ("Pivot", "XYPath") in tool_mods
    # Scalar splines connected via ConnectInput in declared order.
    scalar_connects = [
        c.args for c in tool.ConnectInput.call_args_list
        if c.args[0] in ("Size", "Angle")
    ]
    assert scalar_connects == [("Size", fake_spline_size), ("Angle", fake_spline_angle)]


def test_apply_transform_animation_skips_unset_fields():
    comp = MagicMock()
    spline = MagicMock()
    comp.AddTool.return_value = spline
    comp.FindTool.return_value = None
    tool = MagicMock()
    tool.GetAttrs.return_value = "X"

    anim = fc.TransformAnimation(size=[(0, 0.0), (24, 1.0)])

    fc.apply_transform_animation(comp, tool, anim)

    # Only one modifier (for Size) added.
    comp.AddTool.assert_called_once_with("BezierSpline")
    tool.ConnectInput.assert_called_once_with("Size", spline)


# --------------------------------------------------------------------------- #
# attach_transform_animation (end-to-end with mocks)
# --------------------------------------------------------------------------- #

def test_attach_transform_animation_locks_edits_and_marks_modified():
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["Composition 1"]
    comp = MagicMock()
    ti.LoadFusionCompByName.return_value = comp

    media_in = MagicMock(); media_in.Output = "mi-out"
    media_out = MagicMock()
    xform = MagicMock(); xform.Output = "xf-out"
    xform.GetAttrs.return_value = "SlideShowXf"
    tools = {"MediaIn1": media_in, "MediaOut1": media_out, "SlideShowXf": None}

    def find(name):
        if name == "SlideShowXfAngle":
            return None
        return tools.get(name)

    comp.FindTool.side_effect = find
    spline = MagicMock()
    # AddTool: first call adds Transform, second call adds the BezierSpline
    comp.AddTool.side_effect = [xform, spline]

    events = []
    comp.Lock.side_effect = lambda: events.append("lock")
    comp.Unlock.side_effect = lambda: events.append("unlock")
    orig_add = comp.AddTool.side_effect
    comp.SetAttrs.side_effect = lambda *a, **kw: events.append("setattrs")

    anim = fc.TransformAnimation(angle=[(0, 0.0), (24, 90.0)])
    result = fc.attach_transform_animation(ti, anim)

    assert result is xform
    # Comp was loaded via the canonical (working) call.
    ti.LoadFusionCompByName.assert_called_once_with("Composition 1")
    ti.GetFusionCompByName.assert_not_called()
    # Edits bracketed by lock/unlock.
    assert events[0] == "lock"
    assert "unlock" in events
    # SetAttrs (mark_modified) called AFTER Unlock.
    assert events.index("unlock") < events.index("setattrs")
    # And the modified flag was set to True.
    comp.SetAttrs.assert_called_once_with({"COMPB_Modified": True})
    # Spline keyframes are the 0/24 angle animation.
    spline.SetKeyFrames.assert_called_once_with({0: [0.0], 24: [90.0]})


def test_attach_transform_animation_no_animation_still_attaches_chain():
    """Even with an empty TransformAnimation, the comp + Transform are wired."""
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["Composition 1"]
    comp = MagicMock()
    ti.LoadFusionCompByName.return_value = comp
    media_in = MagicMock(); media_in.Output = "mi-out"
    media_out = MagicMock()
    xform = MagicMock(); xform.Output = "xf-out"
    tools = {"MediaIn1": media_in, "MediaOut1": media_out, "SlideShowXf": None}
    comp.FindTool.side_effect = lambda name: tools.get(name)
    comp.AddTool.return_value = xform

    result = fc.attach_transform_animation(ti, fc.TransformAnimation())

    assert result is xform
    xform.ConnectInput.assert_called_once_with("Input", "mi-out")
    media_out.ConnectInput.assert_called_once_with("Input", "xf-out")


# --------------------------------------------------------------------------- #
# list_tools
# --------------------------------------------------------------------------- #

def test_list_tools_returns_names_in_dict_key_order():
    comp = MagicMock()
    t1 = MagicMock(); t1.GetAttrs.return_value = "MediaIn1"
    t2 = MagicMock(); t2.GetAttrs.return_value = "MediaOut1"
    t3 = MagicMock(); t3.GetAttrs.return_value = "SlideShowXf"
    comp.GetToolList.return_value = {1: t1, 2: t3, 3: t2}

    names = fc.list_tools(comp)

    assert names == ["MediaIn1", "SlideShowXf", "MediaOut1"]


def test_list_tools_handles_empty():
    comp = MagicMock()
    comp.GetToolList.return_value = None
    assert fc.list_tools(comp) == []


# --------------------------------------------------------------------------- #
# insert_tool_chain + effect tool helpers
# --------------------------------------------------------------------------- #

class _FakeTool:
    """Mock tool that records its wiring so a chain can be asserted on."""

    def __init__(self, name):
        self.name = name
        self.Output = "{0}-out".format(name)
        self.inputs = {}

    def ConnectInput(self, input_name, source):
        self.inputs[input_name] = source

    def SetInput(self, input_name, value):
        self.inputs[input_name] = value

    def SetAttrs(self, attrs):
        self.name = attrs.get("TOOLS_Name", self.name)

    def GetAttrs(self, key):
        return self.name


class _FakeComp:
    """Comp that hands out :class:`_FakeTool`s and remembers what was added."""

    def __init__(self, existing=("MediaIn1", "MediaOut1")):
        self.tools = {name: _FakeTool(name) for name in existing}
        self.added = []

    def FindTool(self, name):
        return self.tools.get(name)

    def AddTool(self, tool_type, x=0, y=0):
        tool = _FakeTool(tool_type)
        self.added.append((tool_type, x, y))
        # Fusion names the tool when SetAttrs runs; register under both so a
        # later FindTool by our chosen name succeeds.
        self.tools[tool_type] = tool
        original_setattrs = tool.SetAttrs

        def register(attrs):
            original_setattrs(attrs)
            self.tools[tool.name] = tool

        tool.SetAttrs = register
        return tool


def test_insert_tool_chain_wires_tools_in_order():
    comp = _FakeComp()

    tools = fc.insert_tool_chain(
        comp,
        [("Blur", "B"), ("Pixelate", "P"), ("Transform", "X")],
    )

    assert [t.name for t in tools] == ["B", "P", "X"]
    assert tools[0].inputs["Input"] == "MediaIn1-out"
    assert tools[1].inputs["Input"] == "Blur-out"
    assert tools[2].inputs["Input"] == "Pixelate-out"
    assert comp.tools["MediaOut1"].inputs["Input"] == "Transform-out"
    assert comp.added == [("Blur", 1, 0), ("Pixelate", 2, 0), ("Transform", 3, 0)]


def test_insert_tool_chain_with_no_specs_wires_media_in_to_media_out():
    comp = _FakeComp()
    assert fc.insert_tool_chain(comp, []) == []
    assert comp.tools["MediaOut1"].inputs["Input"] == "MediaIn1-out"
    assert comp.added == []


def test_insert_tool_chain_reuses_existing_tools():
    comp = _FakeComp(existing=("MediaIn1", "MediaOut1", "B"))
    tools = fc.insert_tool_chain(comp, [("Blur", "B")])
    assert tools[0] is comp.tools["B"]
    assert comp.added == []
    assert comp.tools["MediaOut1"].inputs["Input"] == "B-out"


def test_insert_tool_chain_requires_media_io():
    with pytest.raises(RuntimeError, match="MediaIn1"):
        fc.insert_tool_chain(_FakeComp(existing=()), [])
    with pytest.raises(RuntimeError, match="MediaOut1"):
        fc.insert_tool_chain(_FakeComp(existing=("MediaIn1",)), [])


def test_insert_transform_chain_still_returns_the_transform():
    comp = _FakeComp()
    xform = fc.insert_transform_chain(comp)
    assert xform.name == fc.DEFAULT_TRANSFORM_NAME
    assert xform.inputs["Input"] == "MediaIn1-out"
    assert comp.tools["MediaOut1"].inputs["Input"] == "Transform-out"


def test_add_background_sets_each_colour_channel():
    comp = _FakeComp()
    bg = fc.add_background(comp, (0.25, 0.5, 0.75))
    assert bg.name == fc.DEFAULT_BACKGROUND_NAME
    assert bg.inputs == {
        "TopLeftRed": 0.25,
        "TopLeftGreen": 0.5,
        "TopLeftBlue": 0.75,
        "TopLeftAlpha": 1.0,
    }


def test_add_blur_and_pixelate_use_stable_names():
    comp = _FakeComp()
    blur = fc.add_blur(comp)
    pixelate = fc.add_pixelate(comp)
    assert blur.name == fc.DEFAULT_BLUR_NAME
    assert pixelate.name == fc.DEFAULT_PIXELATE_NAME
    # Second call reuses rather than duplicating.
    assert fc.add_blur(comp) is blur
    assert comp.added == [("Blur", 0, 0), ("Pixelate", 0, 0)]


def test_add_merge_wires_background_and_foreground():
    comp = _FakeComp()
    bg = fc.add_background(comp, (0.0, 0.0, 0.0))
    fg = fc.add_blur(comp)

    merge = fc.add_merge(comp, background=bg, foreground=fg, apply_mode="add")

    assert merge.inputs["Background"] == "Background-out"
    assert merge.inputs["Foreground"] == "Blur-out"
    assert merge.inputs["ApplyMode"] == "Add"


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("normal", "Normal"),
        ("add", "Add"),
        ("non_add", "Maximum"),
        ("nonsense", "Normal"),
    ],
)
def test_set_merge_apply_mode(mode, expected):
    merge = _FakeTool("Merge1")
    fc.set_merge_apply_mode(merge, mode)
    assert merge.inputs["ApplyMode"] == expected


