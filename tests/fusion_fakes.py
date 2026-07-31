"""Shared behaviour for the fake Fusion tools the applier tests build on.

The 3D page-turn graph *reads* values back out of Fusion (the renderer's
resolution, the camera's angle of view) and derives the camera distance from
them, so a fake that only records ``SetInput`` calls is no longer enough to
test it. These helpers give the fakes the small amount of real Fusion
behaviour that maths depends on:

* ``Renderer3D`` defaults its ``Width``/``Height`` to the source image, not
  the timeline. 1920x1080 stands in for "whatever the media is".
* ``Camera3D`` derives ``AoV`` from ``FLength`` and the aperture rather than
  storing it, so changing the focal length changes the angle of view. Getting
  this wrong in the fake would hide a wrong camera distance in the real graph.
"""

from __future__ import annotations

import math


#: Vertical aperture of Fusion's default film gate (BMD_URSA_4K_16x9), in
#: inches. Read off Resolve Studio 20.3.3.
FAKE_APERTURE_H_INCHES = 0.4677165354330709

#: Inputs a freshly added tool of each type reports before we touch it.
FAKE_TOOL_DEFAULTS = {
    "Renderer3D": {"Width": 1920.0, "Height": 1080.0},
    "Camera3D": {"FLength": 35.0, "ApertureH": FAKE_APERTURE_H_INCHES},
    "Shape3D": {
        "SurfacePlaneInputs.SizeLock": 1.0,
        "SurfacePlaneInputs.Width": 1.0,
        "SurfacePlaneInputs.Height": 1.0,
    },
}


def fake_get_input(inputs, input_name):
    """Stand in for ``Tool.GetInput``, deriving ``AoV`` the way Fusion does."""
    if input_name == "AoV" and "FLength" in inputs and "ApertureH" in inputs:
        focal_mm = float(inputs["FLength"])
        aperture_mm = float(inputs["ApertureH"]) * 25.4
        return math.degrees(2.0 * math.atan((aperture_mm / 2.0) / focal_mm))
    return inputs.get(input_name)
