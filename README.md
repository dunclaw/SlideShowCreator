# SlideShowCreator

A DaVinci Resolve plug-in (Python script) for quickly building polished slideshow timelines from a list of media — inspired by Windows Movie Maker, but extended with audio analysis, beat-synced transitions, and rich Fusion-driven animations.

## Goals

1. **Ordered media → timeline + transitions.** Drop media into an ordered list; the plug-in builds a Resolve timeline with chosen transitions (push, slide, flip, dissolve, fade, drop, etc.), or an "Auto" mix.
2. **Soundtrack analysis & sync.** Analyze a music track for length, BPM, beats, and downbeats; optionally snap transition points to musical hits.
3. **Titles.** Insert titles at flagged media items via Resolve's Fusion title generators.
4. **Bulk duration adjustment.** Hit a target total length without editing each clip individually.

## Status

Project bootstrap. See `docs/PLAN.md` (or the session plan) for the implementation roadmap.

## Target environment

- DaVinci Resolve 18+ (free or Studio) on Windows / macOS / Linux
- Python 3.6+ (64-bit) — required by Resolve's scripting API
- Installed via the `Workspace → Scripts → Edit` menu

## License

TBD
