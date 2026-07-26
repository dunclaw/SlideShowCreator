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

### The alternating V1/V2 layout (implemented in `slideshow.layout`)

Rather than "slides on V1, transitions on V2", the builder alternates: slide
0 on V1, slide 1 on V2, slide 2 on V1, … Adjacent pairs then always live on
different tracks and can overlap by the transition duration.

```
V2          ┌────────────┐            ┌────────────┐
V1  ┌───────┼──┐      ┌──┼────────────┼──┐
    │ slide 0  │      │ slide 2       │  │
    └───────┼──┘      └──┼────────────┼──┘
            │ slide 1    │            │ slide 3
            └────────────┘            └────────────┘
```

Two consequences worth remembering:

- Each clip is the **incoming** side of the transition before it *and* the
  **outgoing** side of the transition after it. Both sets of keyframes have
  to land in that clip's single Fusion comp, which is why
  `transitions.applier.merge_clip_plans` exists — applying the two plans
  separately would clobber the first one's Transform keyframes.
- Overlaps must be clamped so a clip's two transitions never meet in the
  middle. `slideshow.layout` shrinks them proportionally until every clip
  keeps at least one frame to itself.

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
| pixelate | `Pixelate` | `XPixelSize` |
| opacity | `Transform` | `Blend` |
| fade to colour | `Merge` over a `Background` | `Merge.Blend` |

Fading the **Merge** rather than the Transform is what makes a dip-to-colour
transition reveal the colour instead of fading to transparency.

⚠️ The two input names above are taken from Fusion's documented tool
reference and have **not yet been confirmed against a live Resolve build** —
run `scripts/probe_composite.py` and correct this table.

### Open question: additive / non-additive dissolves

`additive_dissolve` and `non_additive_dissolve` need the incoming clip to
composite onto the outgoing one with an Add / Maximum blend. A per-clip
Fusion comp **cannot see the clip beneath it on the timeline**, so a Fusion
`Merge` can't express this — the applier falls back to
`TimelineItem.SetProperty("CompositeMode", …)` at the Edit-page level
(`"Add"` / `"Lighten"`). Those property values are unverified; the probe
script enumerates which ones Resolve accepts.


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
