"""Unit tests for :mod:`slideshow.transitions.applier`.

Two halves:

* ``merge_clip_plans`` — pure keyframe arithmetic, no mocks needed.
* ``build_comp_graph`` / ``apply_comp_spec`` — assert the Fusion API calls
  we make, using the same fake-comp approach as ``test_fusion_comps.py``.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from slideshow import fusion_comps as fc
from slideshow.layout import PlacedClip
from slideshow.transitions import plan_transition, registered_kinds
from slideshow.transitions.applier import (
    TIMELINE_COMPOSITE_MODES,
    CompSpec,
    apply_comp_spec,
    apply_composite_mode,
    build_comp_graph,
    comp_spec_for_clip,
    merge_clip_plans,
)
from slideshow.transitions.base import ClipPlan, TransitionPlan
from slideshow.fusion_comps import PIXELATE_TOOL, TransformAnimation
from slideshow.project_model import TransitionChoice
from tests.fusion_fakes import FAKE_TOOL_DEFAULTS, fake_get_input


# --------------------------------------------------------------------------- #
# merge_clip_plans
# --------------------------------------------------------------------------- #

def test_merge_of_nothing_is_empty():
    spec = merge_clip_plans(length_frames=96)
    assert spec.is_empty()
    assert spec.length_frames == 96
    assert spec.transform is None
    assert spec.blend is None


def test_lead_in_keyframes_start_at_clip_frame_zero():
    lead_in = ClipPlan(blend=[(0, 0.0), (12, 1.0)])
    spec = merge_clip_plans(length_frames=96, lead_in=lead_in)
    assert spec.blend == [(0, 0.0), (12, 1.0)]
    assert not spec.is_empty()


def test_lead_out_keyframes_are_shifted_to_the_end_of_the_clip():
    lead_out = ClipPlan(blend=[(0, 1.0), (12, 0.0)])
    spec = merge_clip_plans(
        length_frames=96, lead_out=lead_out, lead_out_frames=12
    )
    assert spec.blend == [(84, 1.0), (96, 0.0)]


def test_both_halves_merge_into_one_channel():
    lead_in = ClipPlan(blend=[(0, 0.0), (12, 1.0)])
    lead_out = ClipPlan(blend=[(0, 1.0), (12, 0.0)])
    spec = merge_clip_plans(
        length_frames=96, lead_in=lead_in, lead_out=lead_out, lead_out_frames=12
    )
    assert spec.blend == [(0, 0.0), (12, 1.0), (84, 1.0), (96, 0.0)]


def test_hold_keyframe_inserted_when_the_two_halves_disagree():
    # Lead-in leaves Size at 1.0; lead-out wants to start from 2.0.
    lead_in = ClipPlan(transform=TransformAnimation(size=[(0, 0.0), (10, 1.0)]))
    lead_out = ClipPlan(transform=TransformAnimation(size=[(0, 2.0), (10, 3.0)]))
    spec = merge_clip_plans(
        length_frames=50, lead_in=lead_in, lead_out=lead_out, lead_out_frames=10
    )
    assert spec.transform.size == [(0, 0.0), (10, 1.0), (39, 1.0), (40, 2.0), (50, 3.0)]


def test_no_hold_keyframe_when_the_halves_already_agree():
    lead_in = ClipPlan(transform=TransformAnimation(size=[(0, 0.0), (10, 1.0)]))
    lead_out = ClipPlan(transform=TransformAnimation(size=[(0, 1.0), (10, 2.0)]))
    spec = merge_clip_plans(
        length_frames=50, lead_in=lead_in, lead_out=lead_out, lead_out_frames=10
    )
    assert spec.transform.size == [(0, 0.0), (10, 1.0), (40, 1.0), (50, 2.0)]


def test_colliding_frames_resolve_in_favour_of_the_lead_out():
    lead_in = ClipPlan(blend=[(0, 0.0), (10, 0.5)])
    lead_out = ClipPlan(blend=[(0, 1.0), (5, 0.0)])
    spec = merge_clip_plans(
        length_frames=10, lead_in=lead_in, lead_out=lead_out, lead_out_frames=10
    )
    # lead_out offset is 0, so frame 0 collides — the lead-out value wins.
    assert spec.blend[0] == (0, 1.0)


def test_point_channels_merge_and_shift():
    lead_in = ClipPlan(
        transform=TransformAnimation(center=[(0, (-0.5, 0.5)), (10, (0.5, 0.5))])
    )
    lead_out = ClipPlan(
        transform=TransformAnimation(center=[(0, (0.5, 0.5)), (10, (1.5, 0.5))])
    )
    spec = merge_clip_plans(
        length_frames=100, lead_in=lead_in, lead_out=lead_out, lead_out_frames=10
    )
    assert spec.transform.center == [
        (0, (-0.5, 0.5)), (10, (0.5, 0.5)), (90, (0.5, 0.5)), (100, (1.5, 0.5))
    ]


def test_all_transform_channels_survive_the_merge():
    lead_in = ClipPlan(
        transform=TransformAnimation(
            center=[(0, (0.0, 0.0)), (5, (0.5, 0.5))],
            size=[(0, 0.0), (5, 1.0)],
            angle=[(0, 90.0), (5, 0.0)],
            pivot=[(0, (0.5, 0.5)), (5, (0.5, 0.5))],
        )
    )
    spec = merge_clip_plans(length_frames=40, lead_in=lead_in)
    assert spec.transform.center and spec.transform.size
    assert spec.transform.angle and spec.transform.pivot


def test_blur_and_pixelate_channels_merge_independently():
    lead_in = ClipPlan(blur_size=[(0, 10.0), (6, 0.0)])
    lead_out = ClipPlan(pixelate_size=[(0, 0.0), (6, 20.0)])
    spec = merge_clip_plans(
        length_frames=60, lead_in=lead_in, lead_out=lead_out, lead_out_frames=6
    )
    assert spec.blur_size == [(0, 10.0), (6, 0.0)]
    assert spec.pixelate_size == [(54, 0.0), (60, 20.0)]


def test_background_prefers_the_lead_in_colour():
    lead_in = ClipPlan(background_color=(0.0, 0.0, 0.0))
    lead_out = ClipPlan(background_color=(1.0, 1.0, 1.0))
    spec = merge_clip_plans(length_frames=10, lead_in=lead_in, lead_out=lead_out)
    assert spec.background_color == (0.0, 0.0, 0.0)


def test_background_falls_back_to_the_lead_out_colour():
    lead_out = ClipPlan(background_color=(0.5, 0.5, 0.5))
    spec = merge_clip_plans(length_frames=10, lead_out=lead_out)
    assert spec.background_color == (0.5, 0.5, 0.5)


def test_composite_mode_prefers_the_incoming_half():
    lead_in = ClipPlan(composite_mode="add")
    lead_out = ClipPlan(composite_mode="non_add")
    assert merge_clip_plans(
        length_frames=10, lead_in=lead_in, lead_out=lead_out
    ).composite_mode == "add"


def test_composite_mode_falls_back_to_the_outgoing_half():
    # A mirrored plan puts the composite mode on the lead-out, because that
    # is the half sitting on the upper track.
    lead_out = ClipPlan(composite_mode="non_add")
    assert merge_clip_plans(
        length_frames=10, lead_out=lead_out
    ).composite_mode == "non_add"


def test_negative_lengths_are_rejected():
    with pytest.raises(ValueError, match="length_frames"):
        merge_clip_plans(length_frames=-1)
    with pytest.raises(ValueError, match="lead_out_frames"):
        merge_clip_plans(length_frames=10, lead_out_frames=-1)


def test_lead_out_longer_than_the_clip_clamps_to_frame_zero():
    lead_out = ClipPlan(blend=[(0, 1.0), (20, 0.0)])
    spec = merge_clip_plans(
        length_frames=10, lead_out=lead_out, lead_out_frames=20
    )
    assert spec.blend == [(0, 1.0), (20, 0.0)]


# --------------------------------------------------------------------------- #
# comp_spec_for_clip
# --------------------------------------------------------------------------- #

def test_comp_spec_for_clip_picks_the_right_half_of_each_plan():
    incoming_marker = ClipPlan(blend=[(0, 0.0), (10, 1.0)])
    outgoing_marker = ClipPlan(blend=[(0, 1.0), (10, 0.0)])
    lead_in_plan = TransitionPlan(
        kind="dissolve", duration_frames=10,
        incoming=incoming_marker, outgoing=ClipPlan(blur_size=[(0, 9.0)]),
    )
    lead_out_plan = TransitionPlan(
        kind="dissolve", duration_frames=10,
        incoming=ClipPlan(blur_size=[(0, 9.0)]), outgoing=outgoing_marker,
    )
    clip = PlacedClip(
        index=1, track_index=2, record_frame=0, length_frames=50,
        lead_in_frames=10, lead_out_frames=10,
    )

    spec = comp_spec_for_clip(
        clip, lead_in_plan=lead_in_plan, lead_out_plan=lead_out_plan
    )

    # Only the incoming/outgoing halves were used — no blur leaked in.
    assert spec.blur_size is None
    assert spec.blend == [(0, 0.0), (10, 1.0), (40, 1.0), (50, 0.0)]
    assert spec.length_frames == 50


def test_comp_spec_for_a_real_slide_transition():
    choice = TransitionChoice(kind="slide_left", duration_frames=12)
    plan = plan_transition(choice)
    clip = PlacedClip(
        index=1, track_index=2, record_frame=0, length_frames=96,
        lead_in_frames=12,
    )
    spec = comp_spec_for_clip(clip, lead_in_plan=plan)
    assert spec.transform.center == [(0, (1.5, 0.5)), (12, (0.5, 0.5))]


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #

class _FakeInputHandle:
    """Stands in for a Fusion Input that has a modifier connected."""

    def __init__(self, tool):
        self.tool = tool

    def GetConnectedOutput(self):
        out = MagicMock(name="connected-output")
        out.GetTool.return_value = self.tool
        return out


class _FakeTool:
    def __init__(self, name, comp=None):
        self.name = name
        self.comp = comp
        self.Output = "{0}-out".format(name)
        self.inputs = dict(FAKE_TOOL_DEFAULTS.get(name, {}))
        self.connections = {}
        self.keyframes = None

    def ConnectInput(self, input_name, source):
        self.connections[input_name] = source
        # Fusion renames a modifier to "<tool><input>" when it is connected;
        # our helpers rely on that to find it again instead of duplicating.
        if isinstance(source, _FakeTool) and source.name == "BezierSpline":
            source.name = "{0}{1}".format(self.name, input_name)
            if self.comp is not None:
                self.comp.tools[source.name] = source

    def AddModifier(self, input_name, kind):
        modifier = _FakeTool(kind, comp=self.comp)
        setattr(self, input_name, _FakeInputHandle(modifier))
        return True

    def SetKeyFrames(self, keyframes):
        self.keyframes = keyframes

    def SetInput(self, input_name, value):
        self.inputs[input_name] = value

    def GetInput(self, input_name, frame=None):
        return fake_get_input(self.inputs, input_name)

    def SetAttrs(self, attrs):
        self.name = attrs.get("TOOLS_Name", self.name)

    def GetAttrs(self, key):
        return self.name


class _FakeComp:
    def __init__(self):
        self.tools = {n: _FakeTool(n, comp=self) for n in ("MediaIn1", "MediaOut1")}
        self.added = []
        self.locked = 0
        self.attrs = {}

    def FindTool(self, name):
        return self.tools.get(name)

    def AddTool(self, tool_type, x=0, y=0):
        tool = _FakeTool(tool_type, comp=self)
        self.added.append(tool_type)
        self.tools[tool_type] = tool
        base_setattrs = tool.SetAttrs

        def register(attrs):
            base_setattrs(attrs)
            self.tools[tool.name] = tool

        tool.SetAttrs = register
        return tool

    def Lock(self):
        self.locked += 1

    def Unlock(self):
        self.locked -= 1

    def SetAttrs(self, attrs):
        self.attrs.update(attrs)


def _timeline_item_for(comp):
    ti = MagicMock()
    ti.GetFusionCompNameList.return_value = ["Composition 1"]
    ti.LoadFusionCompByName.return_value = comp
    return ti


def test_graph_is_just_a_transform_for_a_plain_move():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        transform=TransformAnimation(center=[(0, (0.0, 0.5)), (10, (0.5, 0.5))]),
    )

    built = build_comp_graph(comp, spec)

    assert set(built) == {"transform"}
    assert comp.added == ["Transform"]  # the XYPath arrives via AddModifier
    assert built["transform"].connections["Input"] == "MediaIn1-out"
    assert comp.tools["MediaOut1"].connections["Input"] == "Transform-out"


def test_blur_and_pixelate_are_inserted_upstream_of_the_transform():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        blur_size=[(0, 10.0), (10, 0.0)],
        pixelate_size=[(0, 20.0), (10, 0.0)],
    )

    built = build_comp_graph(comp, spec)

    assert set(built) >= {"blur", "pixelate", "transform"}
    assert built["blur"].connections["Input"] == "MediaIn1-out"
    # The pixelate tool is a ResolveFX OFX plugin, whose image input is Source.
    assert built["pixelate"].connections["Source"] == "Blur-out"
    assert built["transform"].connections["Input"] == "{0}-out".format(PIXELATE_TOOL)
    assert comp.tools["MediaOut1"].connections["Input"] == "Transform-out"


def test_blur_only_skips_the_pixelate_node():
    comp = _FakeComp()
    spec = CompSpec(length_frames=50, blur_size=[(0, 10.0), (10, 0.0)])
    built = build_comp_graph(comp, spec)
    assert "pixelate" not in built
    assert built["transform"].connections["Input"] == "Blur-out"


def test_background_colour_inserts_a_dip_merge_under_the_opacity_merge():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        background_color=(0.0, 0.0, 0.0),
        blend=[(0, 0.0), (10, 1.0)],
    )

    built = build_comp_graph(comp, spec)

    # Inner pair: the clip's image over an opaque colour.
    assert built["color_background"].inputs["TopLeftRed"] == 0.0
    assert built["color_background"].inputs["TopLeftAlpha"] == 1.0
    assert built["color_merge"].connections["Foreground"] == "Transform-out"
    # Outer pair: all of that over transparency, so it can reveal the clip
    # on the track below.
    assert built["background"].inputs["TopLeftAlpha"] == 0.0
    assert built["merge"].connections["Foreground"] == "Merge-out"
    assert comp.tools["MediaOut1"].connections["Input"] == "Merge-out"


def test_blend_goes_on_the_merge_when_a_background_exists():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        background_color=(0.0, 0.0, 0.0),
        blend=[(0, 0.0), (10, 1.0)],
    )
    built = build_comp_graph(comp, spec)
    # Two keyframes → a BezierSpline is connected to Merge.Blend.
    assert "Blend" in built["merge"].connections
    assert "Blend" not in built["transform"].connections


def test_blend_uses_a_merge_over_a_transparent_background():
    """Transform.Blend crossfades a tool against its own output, so on an
    identity transform it does nothing at all. Opacity must come from a Merge."""
    comp = _FakeComp()
    spec = CompSpec(length_frames=50, blend=[(0, 0.0), (10, 1.0)])
    built = build_comp_graph(comp, spec)

    assert "merge" in built
    assert "Blend" not in built["transform"].connections
    assert "Blend" in built["merge"].connections
    # Transparent background, so the clip underneath shows through.
    assert built["background"].inputs["TopLeftAlpha"] == 0.0
    assert comp.tools["MediaOut1"].connections["Input"] == "Merge-out"


def test_dip_colour_lands_on_the_inner_background():
    """A dip-to-colour needs a solid background to be revealed."""
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        blend=[(0, 0.0), (10, 1.0)],
        color_blend=[(0, 0.0), (5, 0.0), (10, 1.0)],
        background_color=(1.0, 0.0, 0.0),
    )
    built = build_comp_graph(comp, spec)

    assert built["color_background"].inputs["TopLeftAlpha"] == 1.0
    assert built["color_background"].inputs["TopLeftRed"] == 1.0
    # The two blends drive different merges and must not be confused.
    assert "Blend" in built["color_merge"].connections
    assert "Blend" in built["merge"].connections


def test_background_colour_without_blend_still_wires_to_media_out():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        background_color=(0.0, 0.0, 0.0),
        color_blend=[(0, 1.0), (10, 0.0)],
    )
    built = build_comp_graph(comp, spec)
    assert "merge" not in built
    assert comp.tools["MediaOut1"].connections["Input"] == "Merge-out"
    assert built["color_merge"].connections["Foreground"] == "Transform-out"


def test_single_blend_keyframe_sets_a_constant():
    comp = _FakeComp()
    spec = CompSpec(length_frames=50, blend=[(0, 0.5)])
    built = build_comp_graph(comp, spec)
    assert built["merge"].inputs["Blend"] == 0.5


def test_no_blend_and_no_background_leaves_the_transform_wired_to_output():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50, transform=TransformAnimation(size=[(0, 0.5), (10, 1.0)])
    )
    built = build_comp_graph(comp, spec)
    assert "merge" not in built
    assert comp.tools["MediaOut1"].connections["Input"] == "Transform-out"


def test_rebuilding_the_same_graph_does_not_duplicate_tools():
    comp = _FakeComp()
    spec = CompSpec(
        length_frames=50,
        blur_size=[(0, 10.0), (10, 0.0)],
        blend=[(0, 0.0), (10, 1.0)],
        background_color=(0.0, 0.0, 0.0),
    )
    first = build_comp_graph(comp, spec)
    added_after_first = list(comp.added)
    second = build_comp_graph(comp, spec)

    assert comp.added == added_after_first
    assert second["blur"] is first["blur"]
    assert second["merge"] is first["merge"]
    assert second["color_merge"] is first["color_merge"]


def test_blur_keyframes_land_on_the_x_size_input():
    comp = _FakeComp()
    spec = CompSpec(length_frames=50, blur_size=[(0, 10.0), (10, 0.0)])
    built = build_comp_graph(comp, spec)
    assert fc.BLUR_SIZE_INPUT in built["blur"].connections


# --------------------------------------------------------------------------- #
# apply_comp_spec
# --------------------------------------------------------------------------- #

def test_apply_comp_spec_skips_empty_specs_entirely():
    ti = MagicMock()
    assert apply_comp_spec(ti, CompSpec(length_frames=50)) is None
    ti.GetFusionCompNameList.assert_not_called()
    ti.AddFusionComp.assert_not_called()


def test_apply_comp_spec_locks_builds_and_marks_modified():
    comp = _FakeComp()
    ti = _timeline_item_for(comp)
    spec = CompSpec(length_frames=50, blend=[(0, 0.0), (10, 1.0)])

    result = apply_comp_spec(ti, spec)

    assert result is comp
    assert comp.locked == 0  # balanced Lock/Unlock
    assert comp.attrs == {"COMPB_Modified": True}
    assert comp.tools["MediaOut1"].connections["Input"] == "Merge-out"


def test_apply_comp_spec_sets_timeline_composite_mode():
    comp = _FakeComp()
    ti = _timeline_item_for(comp)
    spec = CompSpec(
        length_frames=50, blend=[(0, 0.0), (10, 1.0)], composite_mode="add"
    )

    apply_comp_spec(ti, spec)

    ti.SetProperty.assert_called_once_with("CompositeMode", 1)  # Add


def test_composite_modes_are_integers():
    """SetProperty rejects strings and reports it only by returning False."""
    for value in TIMELINE_COMPOSITE_MODES.values():
        assert isinstance(value, int)


def test_composite_mode_normal_is_a_no_op():
    ti = MagicMock()
    assert apply_composite_mode(ti, "normal") is True
    ti.SetProperty.assert_not_called()


def test_composite_mode_unknown_returns_false():
    ti = MagicMock()
    assert apply_composite_mode(ti, "nonsense") is False
    ti.SetProperty.assert_not_called()


def test_composite_mode_tolerates_resolve_rejecting_the_property():
    ti = MagicMock()
    ti.SetProperty.side_effect = Exception("unsupported")
    assert apply_composite_mode(ti, "add") is False


def test_composite_mode_only_spec_still_builds_nothing_in_the_comp():
    # composite_mode alone makes the spec non-empty, but the graph is a
    # bare pass-through Transform — that's fine and must not crash.
    comp = _FakeComp()
    ti = _timeline_item_for(comp)
    apply_comp_spec(ti, CompSpec(length_frames=10, composite_mode="non_add"))
    ti.SetProperty.assert_called_once_with("CompositeMode", 10)  # Lighten
    assert comp.tools["MediaOut1"].connections["Input"] == "Transform-out"


# --------------------------------------------------------------------------- #
# End-to-end over every registered transition kind
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind", sorted(registered_kinds()))
def test_every_kind_can_be_planned_merged_and_applied(kind):
    plan = plan_transition(TransitionChoice(kind=kind, duration_frames=12))
    clip = PlacedClip(
        index=1, track_index=2, record_frame=0, length_frames=96,
        lead_in_frames=plan.duration_frames, lead_out_frames=plan.duration_frames,
    )
    spec = comp_spec_for_clip(clip, lead_in_plan=plan, lead_out_plan=plan)

    comp = _FakeComp()
    ti = _timeline_item_for(comp)
    apply_comp_spec(ti, spec)

    if not spec.is_empty():
        assert comp.attrs == {"COMPB_Modified": True}
        # Renderer3D is the tail of the page-turn graph; every other kind
        # ends in a Transform, or a Merge when it needs opacity.
        assert comp.tools["MediaOut1"].connections["Input"] in (
            "Transform-out", "Merge-out", "Renderer3D-out"
        )


# --------------------------------------------------------------------------- #
# 3D page turn
# --------------------------------------------------------------------------- #

def _page_spec(**kwargs):
    kwargs.setdefault("angle", [(0, 100.0), (24, 0.0)])
    return CompSpec(length_frames=60, page_turn=fc.PageTurnAnimation(**kwargs))


def test_page_turn_builds_a_3d_scene_instead_of_the_2d_chain():
    comp = _FakeComp()

    built = build_comp_graph(comp, _page_spec())

    assert set(comp.added) == {
        "Shape3D", "Transform3D", "Camera3D", "Merge3D", "Renderer3D",
        "BezierSpline",  # drives the rotation
    }
    assert "Transform" not in comp.added
    # MediaIn textures the plane; the renderer is what MediaOut sees.
    assert built["plane"].connections["MaterialInput"] == "MediaIn1-out"
    assert built["transform"].connections["SceneInput"] == "Shape3D-out"
    assert built["merge"].connections["SceneInput1"] == "Transform3D-out"
    assert built["merge"].connections["SceneInput2"] == "Camera3D-out"
    assert built["renderer"].connections["SceneInput"] == "Merge3D-out"
    assert comp.tools["MediaOut1"].connections["Input"] == "Renderer3D-out"


def test_page_plane_is_unit_height_and_matches_the_render_aspect():
    comp = _FakeComp()

    built = build_comp_graph(comp, _page_spec())

    plane = built["plane"]
    # SizeLock has to be cleared first or Width and Height move together.
    assert plane.inputs[fc.PLANE_SIZE_LOCK] == 0.0
    assert plane.inputs[fc.PLANE_HEIGHT] == 1.0
    # The fake renderer reports 1920x1080, like a real one reporting the
    # source resolution rather than the timeline's.
    assert plane.inputs[fc.PLANE_WIDTH] == pytest.approx(1920.0 / 1080.0)
    assert built["plane_size"] == (pytest.approx(16.0 / 9.0), 1.0)


def test_camera_is_fitted_so_a_flat_page_fills_the_frame():
    comp = _FakeComp()

    built = build_comp_graph(comp, _page_spec(focal_length=20.0))

    camera = built["camera"]
    # Half the plane height over the tangent of half the vertical AoV. Getting
    # this wrong renders a correct-looking graph at the wrong scale, which is
    # only visible as a jump at the end of the transition.
    expected = fc.camera_distance(1.0, built["aov"])
    assert camera.inputs[fc.TRANSFORM3D_TRANSLATE_Z] == pytest.approx(expected)
    assert built["camera_distance"] == pytest.approx(expected)


def test_a_frame_filling_page_is_backed_off_until_it_clears_the_camera():
    # The fake renderer reports 16:9, so the plane is 1.778 wide -- wider than
    # the 1.515 the requested lens would put the camera at. Left alone the
    # page sweeps straight through the lens and the render falls apart.
    built = build_comp_graph(_FakeComp(), _page_spec(focal_length=18.0))

    width = built["plane_size"][0]
    assert built["focal_length"] > 18.0
    assert built["camera_distance"] > width
    assert built["camera_distance"] == pytest.approx(width * fc.PAGE_CAMERA_CLEARANCE)
    # The lens the camera actually got is the one we report.
    assert built["camera"].inputs[fc.CAMERA_FOCAL_LENGTH] == pytest.approx(
        built["focal_length"]
    )


def test_a_narrow_page_keeps_the_lens_it_asked_for():
    built = build_comp_graph(_FakeComp(), _page_spec(focal_length=18.0))

    # The clamp is width-driven, so a page narrower than the fitted distance
    # is left alone. Checked directly on the helper because the fake renderer
    # only ever reports 16:9.
    assert fc.page_focal_length_for_clearance(18.0, 1.5152, 0.75) == 18.0
    assert fc.page_focal_length_for_clearance(18.0, 1.5152, 1.3333) == pytest.approx(
        18.0 * 1.3333 * fc.PAGE_CAMERA_CLEARANCE / 1.5152
    )
    assert built["focal_length"] > 18.0


def test_the_clamp_lands_on_the_same_distance_from_any_starting_lens():
    # d = h*F/apertureH, so distance is linear in focal length -- which is
    # what lets us buy clearance by lengthening the lens without changing what
    # a resting page looks like. Whatever lens you start from, the clamp
    # should arrive at the same required distance.
    width = 1.7778
    required = width * fc.PAGE_CAMERA_CLEARANCE
    for focal, fitted in ((18.0, 1.5152), (12.0, 1.5152 * 12.0 / 18.0)):
        clamped = fc.page_focal_length_for_clearance(focal, fitted, width)
        assert fitted * clamped / focal == pytest.approx(required)


def test_page_focal_length_rejects_nonsense():
    with pytest.raises(ValueError):
        fc.page_focal_length_for_clearance(0.0, 1.5, 1.0)
    with pytest.raises(ValueError):
        fc.page_focal_length_for_clearance(18.0, 0.0, 1.0)


def test_entry_angle_depends_only_on_width_over_distance():
    # tan(theta) = 2d/W, so the height cancels and doubling both leaves it put.
    assert fc.page_entry_angle(1.0, 2.0) == pytest.approx(
        fc.page_entry_angle(2.0, 4.0)
    )
    # A wider page enters later -- it has further to swing before its free
    # edge comes back inside the frame.
    assert fc.page_entry_angle(1.7778, 2.0444) < fc.page_entry_angle(0.75, 1.5152)
    assert fc.page_entry_angle(1.7778, 2.0444) == pytest.approx(66.5, abs=0.5)
    assert fc.page_entry_angle(0.75, 1.5152) == pytest.approx(76.1, abs=0.5)


def test_entry_angle_rejects_nonsense():
    with pytest.raises(ValueError):
        fc.page_entry_angle(0.0, 1.5)
    with pytest.raises(ValueError):
        fc.page_entry_angle(1.0, 0.0)


def test_angles_are_scaled_into_the_visible_range():
    keys = [(0, 100.0), (8, 86.0), (16, 45.0), (30, 0.0)]

    fitted = fc.fit_angles_to_frame(keys, 66.5)

    assert [f for f, _ in fitted] == [0, 8, 16, 30]
    assert fitted[0][1] == pytest.approx(66.5)
    # Scaled, not clipped: the eased shape survives intact.
    assert fitted[1][1] == pytest.approx(86.0 * 0.665)
    # And it still rests at exactly flat, or the cut into the body segment
    # shows a jump.
    assert fitted[-1][1] == 0.0


def test_angles_already_in_range_are_left_alone():
    keys = [(0, 60.0), (10, 20.0), (24, 0.0)]

    assert fc.fit_angles_to_frame(keys, 66.5) == keys
    assert fc.fit_angles_to_frame([], 66.5) == []


def test_negative_angles_are_scaled_by_magnitude():
    # page_turn_away sweeps negative; scaling has to key off the magnitude or
    # the away variant would be left unfitted.
    fitted = fc.fit_angles_to_frame([(0, -100.0), (30, 0.0)], 66.5)

    assert fitted[0][1] == pytest.approx(-66.5)


def test_the_built_page_uses_the_fitted_angles():
    built = build_comp_graph(_FakeComp(), _page_spec())

    peak = max(abs(value) for _, value in built["angle"])
    assert peak == pytest.approx(built["entry_angle"])
    assert built["angle"][-1][1] == 0.0


def test_a_shorter_lens_brings_the_camera_closer():
    wide = build_comp_graph(_FakeComp(), _page_spec(focal_length=14.0))
    long = build_comp_graph(_FakeComp(), _page_spec(focal_length=35.0))

    assert wide["aov"] > long["aov"]
    assert wide["camera_distance"] < long["camera_distance"]


@pytest.mark.parametrize(
    "hinge,sign", [("right", 1.0), ("left", -1.0)]
)
def test_hinge_picks_which_edge_the_page_pivots_on(hinge, sign):
    comp = _FakeComp()

    built = build_comp_graph(comp, _page_spec(hinge=hinge))

    half_width = built["plane_size"][0] / 2.0
    assert built["transform"].inputs[fc.TRANSFORM3D_PIVOT_X] == pytest.approx(
        sign * half_width
    )


def test_backface_culling_is_on_by_default_and_can_be_turned_off():
    on = build_comp_graph(_FakeComp(), _page_spec())
    off = build_comp_graph(_FakeComp(), _page_spec(cull_backface=False))

    assert on["plane"].inputs[fc.PLANE_CULL_BACKFACE] == 1.0
    assert off["plane"].inputs[fc.PLANE_CULL_BACKFACE] == 0.0
    # Lighting is always off: there are no lights, so leaving it on lets the
    # renderer darken the photo.
    assert on["plane"].inputs[fc.PLANE_LIT] == 0.0


def test_page_turn_graph_is_rebuilt_not_duplicated():
    comp = _FakeComp()

    build_comp_graph(comp, _page_spec())
    build_comp_graph(comp, _page_spec(focal_length=24.0))

    assert comp.added.count("Shape3D") == 1
    assert comp.added.count("Renderer3D") == 1


def test_camera_distance_rejects_nonsense():
    with pytest.raises(ValueError):
        fc.camera_distance(0.0, 30.0)
    with pytest.raises(ValueError):
        fc.camera_distance(1.0, 0.0)


def test_page_turn_survives_the_merge_into_a_comp_spec():
    plan = plan_transition(TransitionChoice(kind="page_turn", duration_frames=18))
    clip = PlacedClip(
        index=1, track_index=2, record_frame=0, length_frames=18,
        lead_in_frames=18, lead_out_frames=0,
    )

    spec = comp_spec_for_clip(clip, lead_in_plan=plan)

    assert spec.page_turn is not None
    assert spec.page_turn.angle[0][0] == 0
    assert spec.page_turn.angle[-1] == (18, 0.0)
