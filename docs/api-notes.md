# Resolve scripting API — captured notes

Source: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\README.txt` (Last Updated: 7 Oct 2025).

## Environment (Windows)

```
RESOLVE_SCRIPT_API = %PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
RESOLVE_SCRIPT_LIB = C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
PYTHONPATH        ;= %RESOLVE_SCRIPT_API%\Modules
```

## Script discovery folders (Windows)

- All users: `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts`
- Per user: `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts`

Sub-folders matter: place under `Edit/` to make the script appear when the Edit page is active.

## Resolve does not bundle a Python interpreter

Worth stating plainly, because assuming otherwise sends the whole design the
wrong way. The scripting README lists under *Prerequisites*:

> DaVinci Resolve scripting requires one of the following to be installed (for
> all users): Lua 5.1, **Python >= 3.6 64-bit**, Python 2.7 64-bit

There is no interpreter inside the install tree — confirmed by looking. So a
script invoked from the Workspace → Scripts menu runs under whatever *system*
Python Resolve discovered at startup, which is an environment we neither
control nor should be installing packages into.

The practical consequence is that **running in-process buys no environment
stability — it loses it.** This machine is the proof: Python 3.12 crashes
loading `fusionscript.dll`, while 3.14 works. Externally we pin a known-good
interpreter; in-process we take whatever Resolve picked.

The API is identical either way (that is the whole point of
`RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` above), so nothing is given up by
driving Resolve from outside. Hence the architecture in issue #9: an external
engine in a sidecar venv, plus a thin stdlib-only launcher in the Scripts
folder that just hands off to it.

A corollary worth keeping in mind: the version number alone does not tell you
an interpreter is usable. Any setup routine has to *verify* a candidate can
import the API and reach a running Resolve.

## Calls we'll rely on

### MediaPool
- `AppendToTimeline([{mediaPoolItem, startFrame, endFrame, mediaType}])` → `[TimelineItem]`
- `CreateTimelineFromClips(name, [...])`
- `CreateEmptyTimeline(name)`
- `ImportTimelineFromFile(filePath, {importOptions})` — supports AAF/EDL/XML/FCPXML/DRT/ADL/OTIO (fallback path if API insertion proves insufficient).

### Timeline
- `AddTrack("video")` / `DeleteTrack("video", index)`
- `GetItemListInTrack("video", index)`
- `InsertTitleIntoTimeline(titleName)` / `InsertFusionTitleIntoTimeline(titleName)`
- `InsertGeneratorIntoTimeline(name)` / `InsertFusionGeneratorIntoTimeline(name)` / `InsertOFXGeneratorIntoTimeline(name)`

### TimelineItem
- `GetProperty(key)` / `SetProperty(key, value)` — keys include `Pan`, `Tilt`, `ZoomX`, `ZoomY`, `ZoomGang`, `RotationAngle`, `AnchorPointX`, `AnchorPointY`, `CompositeMode`, `Opacity` (0–100), `Distortion`.
- `AddFusionComp()` → `fusionComp`
- `GetFusionCompCount()` / `GetFusionCompByIndex(i)` / `LoadFusionCompByName(name)` / `GetFusionCompNameList()`
- `SetClipColor(...)` for visual tagging.

### MediaPool import gotcha

There are **two different APIs** that look similar but behave very differently:

- `MediaStorage.AddItemListToMediaPool(paths)` — only accepts paths that live
  under one of Resolve's **configured Media Storage roots**. For any path
  outside those roots, Resolve pops a "file does not exist" dialog for each
  file (even when the file is plainly there on disk).
- `MediaPool.ImportMedia(paths)` — accepts arbitrary absolute filesystem
  paths. This is the right call for a plug-in operating on user-chosen
  folders.

We use forward slashes (`os.path.abspath(p).replace("\\", "/")`) before passing
paths to Resolve on Windows — both APIs accept either style in theory but
forward slashes are noticeably more reliable in practice.

### `SetProperty` is constant-only (confirmed)

The README enumerates every supported `SetProperty` key and **none** accept a
keyframe / time component, nor is there an `AddKeyframe` method on
`TimelineItem`. For animated transitions (scale, position, rotation,
transparency over time) we must use `AddFusionComp()` plus a Fusion node graph
with animated inputs.

### Per-clip placement with `AppendToTimeline`

The `clipInfo` dict accepted by `MediaPool.AppendToTimeline([{...}])` supports
these keys (per the SDK README):

- `mediaPoolItem` *(required)*
- `startFrame` / `endFrame` — source range
- `mediaType` — `1` = video only, `2` = audio only
- **`trackIndex`** — target track (e.g. `2` for V2)
- **`recordFrame`** — target timeline frame where the clip is placed

This is what makes the **V1/V2 overlap pattern** (used by the transitions
framework) work: place every slide on V1 sequentially, then drop the incoming
half of each transition on V2 at the precise overlap frame, and animate that
V2 clip with a Fusion comp.

### The split-track layout (implemented in `slideshow.layout`)

At each boundary between two slides, exactly **one** of them is lifted onto
**V2** for the length of the overlap, and the other stays on **V1**. That
splits a slide into up to three segments:

- a **head** on **V2** — the incoming transition window, sitting over the
  previous slide;
- a **body** on **V1** — the settled middle;
- a **tail** on **V2** — the outgoing transition window, sitting over the
  next slide.

A slide with neither a head nor a tail is a single **whole** clip on V1.

```
V2          ┌────┐          ┌────┐          ┌────┐
V1  ┌───────┴────┼──────────┴────┼──────────┴────┼────────┐
    │  slide 0   │   slide 1     │   slide 2     │ slide 3│
    └────────────┴───────────────┴───────────────┴────────┘
```

Why this shape rather than alternating slides V1/V2/V1/…:

- **The animated slide is always on top.** Under an alternating layout half
  the slides animated *underneath* an opaque clip and rendered as hard cuts.
- **V1 is contiguous end to end**, so there is never a gap or a hole to see
  through.
- **Every segment cut is invisible.** It lands on a frame where the image is
  static and fully opaque.
- With no transitions at all, every slide is a single V1 clip and the result
  is identical to the old flat layout.

#### Which side gets lifted

Almost every transition animates the **incoming** slide arriving over the
settled one, so the default is to carve a head off the incoming slide. A few
only read the other way round — `page_turn_away`, where a page peels off to
reveal the next photo, is the outgoing slide moving, and it has to be above
the photo it uncovers. Those set
`Transition.PREFERS_OUTGOING_ON_TOP = True`; `plan_layout` asks per boundary
via its `prefers_outgoing_on_top` predicate (the builder passes
`transitions.wants_outgoing_on_top`, composed with its `auto` resolution so
the layout and the plan can't disagree about which kind is running) and
carves a tail off the outgoing slide instead.

Nothing downstream needs to know which happened:

- `timeline_builder._incoming_on_top` compares the two segments' real track
  indices, so a tail boundary reports `False` and `plan_transition` mirrors
  the plan automatically — the animation always lands on the lifted clip.
- The applier keys purely off `lead_in_frames` / `lead_out_frames`, which the
  layout moves onto whichever segment ended up owning each half.

Contiguity survives because each boundary assigns its whole overlap to
exactly one side: `head_next + tail_prev == overlap`, always.

Two consequences worth remembering:

- A **body can carry both** a lead-in and a lead-out — when the slide before
  it animates out *and* the slide after it animates in, both halves stay on
  V1. `transitions.applier.merge_clip_plans` handles that.
- Overlaps must be clamped so a slide's two transitions never meet in the
  middle. `slideshow.layout` shrinks them proportionally until every slide
  keeps at least one frame to itself — which also guarantees a body is never
  shorter than one frame, whichever ends get carved off it.

`TimelineLayout.clips` therefore holds *segments*, not slides: use
`segments_for_index(i)` / `clip_for_index(i)`, and `BuildResult.items_for_index(i)`
/ `item_for_index(i)`, to get back to `project.items`.

### Importing stills: one path per `ImportMedia` call

`MediaPool.ImportMedia()` auto-detects image sequences. Given several
consecutively numbered files in a **single** call it silently collapses them
into one clip:

```python
mp.ImportMedia(["…/DSCF0043.JPG", "…/DSCF0044.JPG", "…/DSCF0045.JPG"])
# -> [<MediaPoolItem "DSCF[0043-0045].JPG">]      one clip, not three
```

For a slideshow that is catastrophic — consecutively numbered stills are the
normal case. Import **one path per call** and Resolve has nothing to form a
sequence from:

```python
for path in paths:
    mp.ImportMedia([path])       # -> exactly one MediaPoolItem each
```

The `{"FilePath":…, "StartIndex":…, "EndIndex":…}` dict form does *not* help:
it returns `[]` for stills.

`ImportMedia` also returns `[]` for a file already in the pool, so a
re-import is not a no-op that returns the existing clip — walk the pool and
match on `File Path` first.

### Still duration: `SetMarkInOut`, not `startFrame` / `endFrame`

`AppendToTimeline`'s `startFrame` / `endFrame` keys are honoured for video but
**silently ignored for stills**. A still's source is a single frame, so any
range is out of bounds and Resolve substitutes the "standard still duration"
user preference (120 frames at 24 fps) instead:

```python
mp.AppendToTimeline([{ "mediaPoolItem": still, "startFrame": 0,
                       "endFrame": 71, "recordFrame": 0, "trackIndex": 1 }])
# -> a 120-frame clip, not 72
```

That is not a cosmetic problem: over-long clips collide with the next
`recordFrame`, and Resolve pushes the collided clip along the track, which
silently destroys an overlapping layout.

`MediaPoolItem.SetMarkInOut(in, out, "video")` *is* honoured. Mark the range,
omit `startFrame`/`endFrame`, and lengths come out exact:

```python
still.SetMarkInOut(0, frames - 1, "video")
mp.AppendToTimeline([{ "mediaPoolItem": still, "recordFrame": 0,
                       "trackIndex": 1, "mediaType": 1 }])
```

Marks persist on the pool item, so clear them with `ClearMarkInOut()` when the
build finishes. Note the `Start`/`End` clip properties of a still are parsed
from its **filename** (`DSCF0085.JPG` → `85`) and are not a usable source
range.

With this in place `recordFrame` and `trackIndex` are honoured exactly —
verified on Resolve Studio 20.3.3 with a three-slide, 24-frame-overlap build:

```
V1  DSCF0085   0..72     72 frames
V2  DSCF0086   48..120   overlaps by 24
V1  DSCF0089   96..168   overlaps by 24
```

### `AppendToTimeline` targets the *current* timeline

`MediaPool.AppendToTimeline` has no timeline argument — it appends to
whatever `Project.GetCurrentTimeline()` returns. After
`CreateEmptyTimeline(name)` you **must** call
`Project.SetCurrentTimeline(timeline)` before appending, otherwise the clips
land on whichever timeline the user happened to have open.

Tracks must also exist before you place onto them: `trackIndex: 2` on a
timeline with a single video track is not self-creating. Call
`Timeline.AddTrack("video")` until `GetTrackCount("video")` is high enough.

### Fusion tools used by the transition applier

The graph the applier builds per clip, inserting only what the merged plan
needs:

```
MediaIn1 ─► [Blur] ─► [Pixelate] ─► Transform ─┬─► MediaOut1
                                               │
                    Background ─► Merge ◄──────┘
                                    └─► MediaOut1   (background variant)
```

| Effect | Tool | Animated input |
| --- | --- | --- |
| blur | `Blur` | `XBlurSize` (LockXY ties Y to it) |
| pixelate | `ofx.com.blackmagicdesign.resolvefx.MosaicBlur` | `PixelFrequency` |
| opacity / fade | `Merge` over a `Background` | `Merge.Blend` |

See "`Blend` is not opacity" below for why fades go on the Merge and never on
the Transform.

Two traps in that table, both confirmed on Resolve Studio 20.3.3:

- **There is no native Fusion pixelate tool.** `comp.AddTool("Pixelate")`,
  `"Pixelize"` and `"Mosaic"` all return `None`. The effect exists only as the
  ResolveFX OFX plugin above, addressed by its full reverse-DNS ID.
- **OFX plugins name their image input `Source`, not `Input`.** Connecting to
  `Input` leaves the tool silently unwired. `fusion_comps.primary_image_input()`
  keeps that mapping in one place.

`PixelFrequency` is also **inverted** relative to the obvious mental model: it
counts cells across the frame, so *larger is finer*, and its default is 100.
Transition plans describe pixelation as a block size (1.0 = untouched, larger
= chunkier), so `fusion_comps.pixel_size_to_frequency()` converts between the
two. Writing a block size straight into `PixelFrequency` inverts the effect,
and a frequency of 1 flattens the whole frame to a single colour.

### `TimelineItem` `CompositeMode` is an integer enum

`SetProperty("CompositeMode", …)` takes an **int**, not a string. Every string
value is rejected — and rejection is quiet, returning `False` rather than
raising:

```python
item.SetProperty("CompositeMode", "Add")   # -> False, no change
item.SetProperty("CompositeMode", 1)       # -> True
```

Resolve accepts any int in `0..31` without validating it against the actual
list of modes, so the mapping cannot be discovered by probing — it has to be
read off the Inspector. The ordering is Photoshop-style, **not** alphabetical.
Confirmed indices: `0` Normal, `1` Add, `10` Lighten, `13` Exclusion,
`22` Vivid Light.

### `Blend` is not opacity

Every Fusion tool has a `Blend` input, and it is tempting to treat it as
opacity. It isn't: it crossfades the tool's **input** against the tool's
**output**. On an identity `Transform` — exactly what a plain dissolve
produces — input and output are the same image, so animating `Transform.Blend`
has *no visible effect whatsoever*. The clips simply cut.

Opacity comes from a `Merge`, whose `Blend` controls foreground opacity
against its background:

```
MediaIn1 ─► Transform ──────────► Merge.Foreground ─► MediaOut1
                                    ▲
                     Background ────┘  Merge.Background
```

- **Cross dissolve** — `Background` with `TopLeftAlpha = 0`. Blend 0 → 1 makes
  the clip's own alpha ramp up, so the clip *underneath on the timeline*
  shows through.
- **Dip to colour** — needs **two** merges. One `Merge` alone can either
  reveal transparency or reveal a colour, never both, and a dip has to do
  both in sequence: transparent (previous slide visible) → opaque colour →
  the new image. So the applier stacks them:

  ```
  MediaIn1 ─► Transform ─► DipMerge.Foreground ─► Merge.Foreground ─► MediaOut1
              Background(colour, alpha 1) ─┘            │
              Background(alpha 0) ─────────────────────┘
  ```

  `DipMerge.Blend` is the image's opacity over the colour; `Merge.Blend` is
  the overall opacity over the clip below. Held at 0 / ramped 0→1 over the
  first half and vice versa over the second, that produces a real dip.

This is why the applier always routes through a Merge when a plan carries
`blend` keyframes, and never writes `Blend` onto the Transform.

### Transitions are stacking-order sensitive

A transition that animates the incoming clip — which is nearly all of them —
is completely invisible when that clip sits underneath an opaque one: it
renders as a hard cut. The original alternating V1/V2 layout put the incoming
clip on the upper track for only every *other* transition, and the symptom
was a slideshow where every second transition worked.

The clip on the higher track has to own the animation. `plan_transition()`
takes `incoming_on_top` and calls `Transition.mirror()` when it's False, so
the outgoing clip animates *away* instead. The default mirror is a
time-reversal with the two halves swapped; `_SlideBase` and `Drop` override
it so the named direction survives, and `_PushBase` mirrors to itself
because its two clips are always edge-to-edge and never overlap.

The split-track layout above removes the problem at source — the incoming
head is *always* on V2 — so in practice nothing is mirrored any more.
Mirroring is kept because it also fixes the *second* half of the bug:
mirroring only makes a transition visible, it doesn't make the lower slide
animate, so under the old layout half the photos still never moved.
`TimelineBuilder._apply_transitions` derives `incoming_on_top` from the real
track indices via `_incoming_on_top()` rather than assuming, which keeps the
layout and transition packages independent.

### Open question: additive / non-additive dissolves

`additive_dissolve` and `non_additive_dissolve` need the incoming clip to
composite onto the outgoing one with an Add / Lighten blend. A per-clip
Fusion comp **cannot see the clip beneath it on the timeline**, so a Fusion
`Merge` can't express this — the applier falls back to
`TimelineItem.SetProperty("CompositeMode", <int>)` at the Edit-page level.
See the integer-enum note above: `0` is Normal, and the indices for Add and
Lighten must be confirmed from the Inspector.


## Fusion scripting

Available via `resolve.Fusion()` for the global Fusion app object and via
per-clip `fusionComp` objects. The standard Fusion Python scripting API
applies in theory — but several documented behaviors are silently broken
in the Resolve 20.3 build of `fusionscript.dll`. The helpers in
`src/slideshow/fusion_comps.py` encode the working pattern.

### Canonical pattern (use this; everything else is broken)

```python
# Get a writable handle to the comp Resolve actually renders on the timeline
names = clip.GetFusionCompNameList() or []
if not names:
    clip.AddFusionComp()
    names = clip.GetFusionCompNameList() or []
comp = clip.LoadFusionCompByName(names[0])   # USE the return value

# Edit inside a Lock/Unlock pair
comp.Lock()
try:
    xf = comp.AddTool("Transform")
    xf.SetAttrs({"TOOLS_Name": "MyXf"})
    media_in  = comp.FindTool("MediaIn1")
    media_out = comp.FindTool("MediaOut1")
    xf.ConnectInput("Input", media_in.Output)
    media_out.ConnectInput("Input", xf.Output)

    # --- Scalar input animation (Angle, Size, Gain, Blend, Opacity ...) ---
    # Use a BezierSpline modifier added directly to the comp, then connect.
    spline = comp.AddTool("BezierSpline")
    spline.SetKeyFrames({0: [0.0], 24: [90.0]})   # frame -> [value]
    xf.ConnectInput("Angle", spline)              # Fusion auto-renames to "MyXfAngle"

    # --- Point input animation (Center, Pivot) ---
    # Plain ConnectInput of an XYPath does NOT work (XYPath.Output is a Path
    # type, not Point). Use tool.AddModifier — it creates AND wires atomically.
    xf.AddModifier("Center", "XYPath")
    xypath = xf.Center.GetConnectedOutput().GetTool()
    xypath.AddModifier("X", "BezierSpline")
    xypath.AddModifier("Y", "BezierSpline")
    x_spline = xypath.X.GetConnectedOutput().GetTool()
    y_spline = xypath.Y.GetConnectedOutput().GetTool()
    x_spline.SetKeyFrames({0: [-0.5], 24: [0.5]})  # off-screen left -> centered
    y_spline.SetKeyFrames({0: [ 0.5], 24: [0.5]})  # Y stays at 0.5
finally:
    comp.Unlock()

# Tell Resolve the comp changed so the Edit page actually re-renders it
comp.SetAttrs({"COMPB_Modified": True})
```

### What's broken in Resolve 20.3 (confirmed by live testing 2026-05-24)

| API                                                | Behavior                            | Workaround |
|----------------------------------------------------|-------------------------------------|------------|
| `clip.AddFusionComp()` return value                | Stub handle; edits don't persist    | Use `LoadFusionCompByName(name)` return value instead |
| `clip.GetFusionCompByName(name)` return value      | Stub handle; edits don't persist    | Same — use `LoadFusionCompByName` |
| `fusion.GetCurrentComp()`                          | Follows UI clip selection, not scripted target | Don't rely on it; use the handle from `LoadFusionCompByName` |
| `clip.RenameFusionCompByName(old, new)`            | Returns `False`, no-op              | Don't rename comps — always edit the first one |
| `tool.SetInput(name, value, time)` with time       | Ignores `time`; only sets a constant | Use a `BezierSpline` modifier + `SetKeyFrames` + `ConnectInput` |
| `tool.Input[time] = value` (subscript syntax)      | `TypeError: 'NoneType' is not callable` | Same — use BezierSpline |
| `fusion.Refresh()` / `fusion.UpdateDisplay()`      | `NoneType` not callable             | Just rely on `COMPB_Modified` flag |
| `comp.Save()` (no args)                            | Pops a "save .comp file" dialog     | Don't call it — `COMPB_Modified` is what we want |
| `clip.DeleteFusionCompByName(activeComp)`          | Can crash Resolve                   | Don't delete the active comp from script |
| `comp.AddTool("XYPath")` + `xf.ConnectInput("Center", xypath)` | Connection appears to succeed but doesn't drive Center (XYPath.Output is type `Path`, not `Point`) | Use `xf.AddModifier("Center", "XYPath")` — wires correctly in one call |
| `comp.AddTool("XYPath")` then `XYPath1X.SetKeyFrames({0:[v0], 24:[v1]})` | Creates 2 keyframes but values are reset to default 0.5/0.5 (child splines of a disconnected XYPath aren't writable scalars) | Same — use `AddModifier` so the children are bound and writable |
| `xypath.SetKeyFrames({"X": {...}, "Y": {...}})` | `NoneType not callable` — XYPath itself has no `SetKeyFrames` | Address children via `xypath.X.GetConnectedOutput().GetTool().SetKeyFrames(...)` |
| `path = comp.AddTool("Path"); path.SetKeyFrames({0:[(x,y)]})` | `NoneType not callable` | Path is a curve-based motion path — different beast; not useful for X/Y keyframes |

### Key facts

- A clip's **first comp** (`GetFusionCompNameList()[0]`) is the *active* one — i.e. the one Resolve uses when rendering the clip on the Edit-page timeline. Other comps on the clip are just alternate stored compositions; they show in the Fusion-page comp dropdown but don't affect playback unless you switch the UI to them.
- After scripted edits, the comp will *not* show the "3 yellow dots" modified marker (and the Edit page won't re-render the clip) until you call `comp.SetAttrs({"COMPB_Modified": True})`. This appears to be the trigger Resolve uses to invalidate its render cache for that clip.
- When you call `tool.ConnectInput(input_name, modifier)`, Fusion auto-renames the modifier to `<tool_name><input_name>` (e.g. connecting a BezierSpline to `SlideShowXf.Angle` renames it `SlideShowXfAngle`). Useful for finding modifiers later to avoid orphans on rebuild.
- `tool.AddModifier(input_name, modifier_kind)` creates the modifier *and* connects it to the named input in one atomic step. It's the **only** reliable way to wire a Point input — plain `ConnectInput` works for scalar splines but silently no-ops for Point inputs because the Output type doesn't match.
- After `tool.AddModifier(...)`, navigate to the new modifier with `tool.<input_name>.GetConnectedOutput().GetTool()`. The same idiom works recursively for `XYPath.X` / `XYPath.Y` once a BezierSpline has been added to each.
- `PyRemoteObject` proxies fake `hasattr` for any name — every attribute lookup succeeds, but invoking unsupported ones raises `TypeError: 'NoneType' object is not callable`. Always guard with try/except instead of `hasattr` checks.
- **Scalar inputs** (Size, Angle, Gain, Blend, Opacity) animate via a single `BezierSpline` modifier with `{frame: [value]}` keyframes.
- **Point inputs** (Center, Pivot) animate via `AddModifier(input, "XYPath")` + per-axis `AddModifier("X" / "Y", "BezierSpline")` on the XYPath; each child spline takes the same `{frame: [value]}` format.
- `comp.Execute(lua_string)` exists as a Lua escape hatch and is confirmed to run in the `LoadFusionCompByName` handle's context — useful if a future input type can't be driven from external Python. Inside Lua: subscript assignment `tool.Center[frame] = {x, y}` is the canonical Point-keyframe idiom. We don't currently need this for the proven scalar+Point patterns.

### The 3D system (page turns)

A `Renderer3D` feeding `MediaOut1` **does** render correctly inside a Resolve timeline-clip comp — a 3D scene is a legitimate way to build a transition, not just a Fusion-page toy. `Transform3D`, `ImagePlane3D`, `Camera3D`, `Merge3D`, `Renderer3D`, `Shape3D` and `Bender3D` are all present and scriptable.

There is **no native page-turn or page-fold tool** in either Fusion or ResolveFX. `fusion.GetRegList(fusion.CT_Tool)` returns 395 tools on Resolve Studio 20.3.3 and none of them match `Page` or `Turn`, so a page turn has to be assembled from the 3D primitives. The graph we use is:

```
MediaIn1 ─► Shape3D ─► Transform3D ─► Merge3D ─► Renderer3D ─► MediaOut1
           (material)   (rotate Y)      ▲
                                    Camera3D
```

Two scale traps, and both fail *silently* — the render succeeds, it is just the wrong size, which on a timeline looks like a mis-timed cut rather than a broken comp:

- **The plane is ~1 world unit, not pixels/100.** `Shape3D`'s `SurfacePlaneInputs.Width` / `.Height` both default to `1.0`. `ImagePlane3D` behaves the same way but hides those inputs (it derives its size from the image and gives you no way to read it back), which is why we use `Shape3D`. Assuming pixels/100 puts the camera ~20× too far away and renders a postage stamp.
- **`Renderer3D` defaults `Width`/`Height` to the *source* image resolution**, not the timeline format. That happens to be what the rest of our pipeline wants — Resolve fits the comp output to the timeline afterwards, exactly as it does for the 2D path — so read it back rather than overriding it. But it means the plane's aspect must be derived from the renderer, not assumed 16:9; a portrait JPG renders 1536×2048.

Set `SurfacePlaneInputs.SizeLock = 0` **before** writing Width and Height, or they move together.

Fitting the camera so a flat page is pixel-identical to the untouched photo:

- `Camera3D.AoV` is **derived, not stored** — it is recomputed from `FLength` and the film back, so set the focal length first and read `AoV` back. With the default `BMD_URSA_4K_16x9` gate, `ApertureH = 0.4677"` and `FLength = 35` gives `AoV = 19.264°` (`2·atan(11.88/2/35)`).
- `AovType = 0` means AoV is **vertical**, and `ResolutionGateFit` defaults to `Height`. So for a unit-height plane: `camZ = 0.5 / tan(AoV/2)` — 2.946 at 35mm, 1.515 at 18mm. Fitting the height fits the width too, as long as the plane's aspect matches the renderer's.
- Fusion's default 35mm lens is too long to read as a fold; the page barely foreshortens. ~18mm is a good default, and below ~12mm the page distorts visibly at the edges.

Other 3D notes:

- Input names on 3D tools are dotted (`Transform3DOp.Rotate.Y`, `SurfacePlaneInputs.Visibility.CullBackFace`). `set_scalar_keyframes` handles them fine — Fusion renames the connected spline to a clean `SlideShowPageXfYRotation` rather than echoing the dots.
- **Turn on `SurfacePlaneInputs.Visibility.CullBackFace`.** Past 90° a plane shows a mirrored copy of its own image, which reads as a glitch rather than as the back of a page.
- **Turn off `SurfacePlaneInputs.Lighting.IsAffectedByLights`.** With no lights in the scene the renderer otherwise darkens the photo.
- Rotating about Y with the pivot on a vertical edge gives a door-style swing. A point at pivot-relative `x` lands at `z' = -x·sin(θ)`, and the camera is at `+Z`, so **the sign that lifts the free edge toward the camera depends on which edge the hinge is**: a right hinge puts the free edge at negative `x` and wants a *positive* angle, a left hinge puts it at positive `x` and wants a *negative* one. Give both the same raw angle and one of them sinks into the screen instead of lifting off it. `PageTurn.plan` negates the angle for a left hinge so that "positive tips the free edge toward the camera" is true of the *free edge*, whichever side it is on.
- Scene geometry is wired with `SceneInput` / `SceneInput1` / `SceneInput2`, not `Input`; the image goes into `Shape3D.MaterialInput`.
- The angle must rest at **exactly 0.0 on the final frame**. The next frame is a different timeline clip showing the untouched photo, so any residual rotation reads as a jump.
- Two transitions use this graph, differing only in which slide moves: `page_turn` rotates the **incoming** photo in (hinged right), and `page_turn_away` rotates the **outgoing** photo out (hinged left, so the free right edge lifts and sweeps left over the spine). The latter is the only transition that needs the outgoing clip on the upper track — see *The split-track layout* above.

#### Page width, camera clearance and the entry angle

The page is sized to the photo, so its width in world units is the photo's aspect over the frame's: a 3:4 portrait is 0.75 wide, a 4:3 landscape 1.333, a native 16:9 photo (or anything under fill/crop framing) exactly 1.778. Two things follow from that width, and both bite hardest exactly where you want the feature to work.

**The page must clear the camera.** Rotation about Y maps `x` into `z` and leaves `y` alone, so the free edge reaches `plane_width` toward the camera at 90° — the *width*, not the corner distance. At 18mm the camera sits at 1.515. A 4:3 photo (1.333) squeaks through with 13% to spare, which is why letterboxed landscape turns look fine. A frame-filling page at 1.778 does **not**: it sweeps through the lens and out behind it, and the render collapses into a smear with no fold at all. Verified — a page turn built from 16:9 sources was unwatchable.

Fix by *lengthening the lens*, not by moving the camera: distance is exactly linear in focal length (`d = (h/2)/tan(aov/2)` and `tan(aov/2) = (apertureH/2)/F`, so `d = h·F/apertureH`), so scaling `F` scales `d` by the same factor and leaves the resting frame pixel-identical. `page_focal_length_for_clearance` does this, with `PAGE_CAMERA_CLEARANCE = 1.15` — deliberately just above the 1.136 that 4:3 already gets for free, so the case that already looks right is left untouched and 16:9 is backed off by the minimum that works (18mm → 24.3mm).

**Most of the arc happens off-screen.** This is the counter-intuitive one. A page swung far over is very close to the camera, and a point close to the camera projects a long way out — so at 80° a frame-filling page is entirely off the *side* of the screen, not edge-on in the middle of it. It slides into frame only when its free edge comes back inside, and covers the frame again within another 20°. Setting the projected free edge equal to the frame edge:

```
(W/2 - W·cos θ)·d = (d - W·sin θ)·(W/2)   →   tan θ = 2d/W
```

The height cancels: the entry angle depends only on width over camera distance. A letterboxed portrait enters at 76°, a frame-filling page at 66.5°. Sweeping from 100° therefore spends the first third of the transition on a page nobody can see and then dumps the entire fold into four or five frames, which reads as a hard wipe.

`page_entry_angle` computes it and `fit_angles_to_frame` rescales the planned sweep so its peak *is* the entry angle. Scaling rather than clipping keeps the eased profile and the exact 0.0 resting angle. This is done in the comp builder, not at plan time, because the plan describes the shape of the motion and only the builder knows the geometry it will be rendered against.

To *see* any of this: gallery stills are unreliable (`GalleryStillAlbum.ExportStills` returns `False` indefinitely once the album has been cleared with `DeleteStills`, while `GrabStill` keeps succeeding, and `Gallery.AddStillAlbum` doesn't exist in 20.3.3). Render instead — `Project.SetRenderSettings({"MarkIn": f, "MarkOut": f, ...})` plus `AddRenderJob` / `StartRendering` is dependable — then pull frames out with `ffmpeg`. Note that `SetCurrentRenderFormatAndCodec("jpg", "None")` silently does nothing after `LoadRenderPreset`, so you get a one-frame `.mov`; extracting from it is easier than fighting the preset.

### Sampling a photograph's colour without decoding it

The plug-in has to run under Resolve's bundled interpreter with the standard library only, so it cannot read a JPEG — but Fusion is already holding the decoded pixels, and it will do the arithmetic if you ask in the right shape.

Collapsing the image to a single pixel *is* an averaging step: a downscale that extreme has to combine every source pixel into the one output pixel. Scaling that pixel back up gives a flat field of the result, which composites like any other Background. `add_image_average` builds it as `BetterResize(1×1) → BetterResize(frame) → BrightnessContrast`.

Two things to know about the resize tool:

- **There is no `Resize` tool.** `comp.AddTool("Resize")` returns `None` in 20.3.3; the registered ID is **`BetterResize`**.
- `KeepAspect` and `UseFrameFormatSettings` both silently override `Width`/`Height`, so both must be cleared on the downscale or the 1×1 request is ignored. On the way back up, setting `UseFrameFormatSettings` is the easiest way to land on exactly the comp's frame size.

The raw average is not usable on its own. Averaging is doubly destructive: it is strongly desaturating, because mixing every hue in the frame together pulls the result toward grey, and it inherits the picture's exposure, so a dim indoor shot averages to a murky near-black. Measured on a real slideshow frame the raw average came out `(69, 63, 59)` — a colour indistinguishable from the mid-grey dip it was meant to improve on.

So keep the hue and throw away the exposure: `BrightnessContrast` with `Saturation` above 1 to make the surviving cast nameable, and `Gamma` above 1 to normalise brightness. Gamma is the right control rather than `Gain` precisely because it is non-linear — it lifts a dark average a long way and a bright one hardly at all, so every slide dips to a comparably lit tint instead of the dip's brightness swinging with the picture.

Take the average from `MediaIn1` rather than from the end of the 2D chain, or a transition that scales or moves the photo will make the dip colour drift while it plays.

## Bridge gotchas

- **`scriptapp("Resolve")` will crash the host interpreter** (Windows: exit code `-1073741819` / `0xC0000005` access violation) if no Resolve process is running — `fusionscript.dll` segfaults rather than returning `None`. Always pre-check for a running Resolve process before calling it (see `slideshow.resolve_bridge.is_resolve_running`).
- The SDK directory **is already installed** by Resolve at `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` — no separate SDK download is required on Windows.
- Verified working from an external Python 3.14 interpreter against **DaVinci Resolve Studio 20.3.1.6** on Windows 11.

### Python version compatibility (observed empirically)

`fusionscript.dll` is built against Python 3's stable ABI (`python3.dll`), so in
theory it works with any Python ≥ 3.6. In practice, certain interpreter builds
crash on `import DaVinciResolveScript`:

| Python  | Result |
| ------- | ------ |
| 3.12.0 (python.org install) | Access violation while loading `fusionscript.dll` |
| 3.14.3 (Windows Python Manager) | Works |

If you see exit code `-1073741819` immediately after the "import" step in
`scripts/diagnose.py`, try a different Python build (3.10 or 3.14 are known
good). The Resolve SDK README still officially lists Python 3.6 / 3.10 as
preferred.

## A clip's comp runs at the *photo's* resolution, not the timeline's

Confirmed live on a 3840x2160 timeline holding a 1536x2048 portrait photo:

```python
mi = comp.FindTool("MediaIn1")
mi.GetAttrs()["TOOLI_ImageWidth"]   # -> 1536   (not 3840)
mi.GetAttrs()["TOOLI_ImageHeight"]  # -> 2048   (not 2160)
```

The letterboxing happens **downstream of the comp**, at the timeline level,
governed by the project setting:

```python
proj.GetSetting("timelineInputResMismatchBehavior")   # -> 'scaleToFit'
```

Two consequences that are easy to get wrong:

* A `Background` added inside the comp covers only the **photo's own box**,
  not the frame. It gets letterboxed along with the photo, so the pillarbox
  bars keep showing whatever is on the track underneath. Any real backdrop
  feature (issue #13) therefore has to make the comp frame-sized first.
* `Renderer3D` defaults its `Width`/`Height` to the *source* resolution for
  the same reason, which is why `build_page_turn_graph` reads them back
  instead of assuming 16:9 — and why a photo's own aspect currently decides
  where the page-turn hinge lands.

## `Loader` works inside Resolve and reports source dimensions

`comp.AddTool("Loader")` succeeds in Resolve 20.3.3 and reads an arbitrary
path off disk — the image does **not** have to be in the media pool or on the
timeline:

```python
ld = comp.AddTool("Loader")
ld.Clip[1] = r"D:\Temp\Pictures\...\DSCF0046.JPG"
ld.GetAttrs()["TOOLIT_Clip_Width"]   # -> {1: 2048}
ld.GetAttrs()["TOOLIT_Clip_Height"]  # -> {1: 1536}
```

Note the attribute names differ from `MediaIn`'s (`TOOLIT_Clip_*`, returning
a dict keyed by 1, versus `TOOLI_Image*` returning a bare int).

This is what makes the `Accumulate` backdrop in #13 practical: a slide's comp
can rebuild the pile of previously-shown photos directly, one `Loader` each,
instead of rendering stills to disk and re-importing them. Reading the source
dimensions without decoding anything in Python is also what the fit/fill
maths needs.

**But `AddTool("Loader")` pops a modal file-open dialog.** It is a plain
Windows file browser, not a Resolve one, and it does not necessarily come to
the front — so from the script's point of view Resolve simply stops
responding: `GetCurrentPage()` starts returning `None`, `OpenPage()` does
nothing, and `LoadFusionCompByName()` returns `None` while project- and
timeline-level queries carry on working normally. Two of these queued up
during a probe here and cancelling them crashed Resolve.

So a scripted Loader has to be created without the browser ever opening.
Untested options, in rough order of promise: build the tool from a serialised
comp fragment (`comp.Paste`), or set `Clip` in the `AddTool` argument table
rather than afterwards. **Verify this before the Accumulate work depends on
it**, and treat any Resolve that has gone quiet as "look for a dialog behind
the main window" rather than "the API is broken".

## Tools that do not exist (probed by `AddTool`, which returns `None`)

`GetRegList` returns opaque `PyRemoteObject`s, so the only reliable way to
test for a tool is to try adding it.

| Asked for | Result |
| --------- | ------ |
| `Resize` | **missing** — the registered ID is `BetterResize` |
| `CustomTool` | **missing** |
| `Loader`, `Transform3D`, `Merge3D`, `ImagePlane3D`, `Camera3D`, `Renderer3D` | OK |
| `Background`, `Merge`, `Transform`, `BetterResize`, `ColorGain`, `Blur`, `Crop`, `DVE` | OK |

## An opaque backdrop belongs on the lower track only

A Fusion comp cannot see the track beneath it, so a frame-filling opaque
Background inside an *upper* clip's comp hides the lower clip completely.
During a transition that is fatal: the outgoing and incoming photos are on
different tracks, so painting the backdrop in both comps blacks out one half
of the transition and the slide appears to jump across a gap.

`plan_layout` guarantees the lower track is contiguous from frame 0 to
`total_frames`, so one backdrop down there sits behind everything for the
whole timeline. Upper clips still get a frame-sized but *transparent* canvas
— they need it for their animation to move in frame pixels, not for anything
they paint.
