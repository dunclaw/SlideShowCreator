# SlideShowCreator

A DaVinci Resolve plug-in (Python script) for quickly building polished slideshow timelines from a list of media — inspired by Windows Movie Maker, but extended with audio analysis, beat-synced transitions, and rich Fusion-driven animations.

## Goals

1. **Ordered media → timeline + transitions.** Drop media into an ordered list; the plug-in builds a Resolve timeline with chosen transitions (push, slide, flip, dissolve, fade, drop, etc.), or an "Auto" mix.
2. **Soundtrack analysis & sync.** Analyze a music track for length, BPM, beats, and downbeats; optionally snap transition points to musical hits.
3. **Titles.** Insert titles at flagged media items via Resolve's Fusion title generators.
4. **Bulk duration adjustment.** Hit a target total length without editing each clip individually.

## Status

Working end-to-end: an ordered list of media becomes a Resolve timeline with
animated transitions between every slide.

- `slideshow.project_model` — the slideshow as data (24 transition kinds,
  motions, titles, audio settings, JSON persistence).
- `slideshow.layout` — pure placement maths: alternating V1/V2 tracks,
  record frames, clamped overlaps.
- `slideshow.transitions` — plans each transition, then merges the two
  plans that meet on a clip and builds its Fusion node graph.
- `slideshow.timeline_builder` — the Resolve plumbing that joins them.

Try it against a running Resolve:

```
py -3.14 scripts\build_demo_timeline.py <folder-of-images> --transition slide_left
py -3.14 scripts\build_demo_timeline.py --list-transitions
```

Still to come: auto-mix, per-clip motion (Ken Burns), bulk duration fitting,
titles, soundtrack analysis / beat sync, and the in-Resolve UI.

## Target environment

- DaVinci Resolve 20+ (free or Studio) on Windows / macOS / Linux
- Python 3.10 or 3.14 (64-bit) — required by Resolve's scripting API.
  **Avoid Python 3.12** on Windows: it crashes loading `fusionscript.dll`.
  Use `py -3.14 ...` (or `py -3.10 ...`) to invoke the helper scripts.
- Installed via the `Workspace → Scripts → Edit` menu

## License

TBD
