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

## Fusion scripting

Available via `resolve.Fusion()` for the global Fusion app object and via
per-clip `fusionComp` objects returned from `TimelineItem.AddFusionComp()` /
`GetFusionCompByName(name)`. The standard Fusion Python scripting API
applies:

- `comp.Lock()` / `comp.Unlock()` — batch edits inside the bracket.
- `comp.FindTool(name)` — fetch an existing tool (e.g. `"MediaIn1"`).
- `comp.AddTool(toolType, x, y)` — add a new tool. Common types we use:
  `"Transform"`, `"Merge"`, `"Background"`, `"Blur"`.
- `dst.ConnectInput(input_name, src.Output)` — wire outputs to inputs.
- `tool.SetInput(name, value, time)` — set a parameter. Calling with the
  same `(name)` at multiple `time` values produces an animated input (Fusion
  auto-creates a BezierSpline modifier behind the scenes). For `Point`-type
  inputs (e.g. `Transform.Center`, `Transform.Pivot`) the value is a
  `(x, y)` tuple in normalized coords (`(0.5, 0.5)` is frame center).

A Resolve-attached comp comes pre-wired with `MediaIn1` and `MediaOut1`;
we just splice tools in between them.

These conventions are implemented as thin helpers in
`src/slideshow/fusion_comps.py` (`attach_or_get_comp`, `insert_transform_chain`,
`set_scalar_keyframes`, `set_point_keyframes`, `TransformAnimation`,
`attach_transform_animation`).

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
