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

### Important assumption to verify
`SetProperty` looks like it sets a *constant* value, not a keyframed one. For animated transitions (scale/position/rotation/transparency over time), we plan to use `AddFusionComp()` and build a Transform node with animated spline inputs.

## Fusion scripting
Available via `resolve.Fusion()` and via per-clip `fusionComp` objects. Standard Fusion scripting API applies — `AddTool`, parameter set, animation via `BezierSpline` / `LinearSpline` modifiers — same as Fusion standalone.

## Bridge gotchas

- **`scriptapp("Resolve")` will crash the host interpreter** (Windows: exit code `-1073741819` / `0xC0000005` access violation) if no Resolve process is running — `fusionscript.dll` segfaults rather than returning `None`. Always pre-check for a running Resolve process before calling it (see `slideshow.resolve_bridge.is_resolve_running`).
- The SDK directory **is already installed** by Resolve at `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` — no separate SDK download is required on Windows.
- Verified working from an external Python 3.12 interpreter: path resolution and SDK module discovery succeed without setting any env vars manually.
