# SlideShowCreator

A DaVinci Resolve slideshow creator with a native UI Manager dialog launched
from Resolve's Scripts menu. Inspired by Windows Movie Maker, with rich
Fusion-driven transitions. Soundtrack analysis, beat sync, and titles are planned.

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

## Use inside Resolve

The menu script is a small, stdlib-only launcher. It starts a separate Python
environment (the **sidecar**), which displays a native Resolve UI Manager dialog
and talks to the running application through its scripting API. No packages are
installed into the Python interpreter Resolve discovered.

Developer setup on Windows, from this checkout (a non-technical first-run setup
wizard is tracked separately in [#14](https://github.com/dunclaw/SlideShowCreator/issues/14)):

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts\install_resolve_launcher.py
```

On macOS/Linux, create the venv with a known-good 64-bit Python and use
`.venv/bin/python` for the two subsequent commands.

The installer copies `SlideShowCreator.py` and an adjacent `SlideShowCreator.json`
into Resolve's **per-user Fusion/Scripts/Utility** directory. The JSON records
the absolute sidecar Python path; the launcher does not depend on Resolve's
working directory. `--scripts-dir <directory>` overrides the destination.
Keep this checkout and venv in place; rerun installation if you move them.

1. Start Resolve and open a project. Enable **Preferences > System > General >
   External scripting using = Local** if your edition exposes that setting.
2. Choose **Workspace > Scripts > SlideShowCreator** (under Utility if your
   version shows it as a submenu). Restart Resolve if the new item is not listed.
3. Browse to an image folder, set seconds per slide, choose a transition, enter
   a timeline name, then click **Create timeline**.

Images in the folder are sorted alphabetically; subfolders are not scanned.
Supported extensions: JPG/JPEG, PNG, TIF/TIFF, BMP, WebP, HEIC (actual import
support depends on Resolve). **Auto currently uses dissolve**, not an auto-mix.
The existing builder controls framing and overlap exactly as in the CLI.

Each build saves a `project.json` before modifying Resolve, under
`SlideShowCreator/projects/<build-id>` in the user's local application data
directory (`%LOCALAPPDATA%` on Windows, `~/Library/Application Support` on macOS,
`$XDG_DATA_HOME` or `~/.local/share` on Linux). The dialog shows the record path.
Future analysis belongs in the sidecar and stores its numeric results in this
JSON; timeline rebuilding must not rerun analysis.

Invalid input and build failures appear in the dialog. A failed build can leave
a partial timeline; inspect it before retrying. If the launcher cannot start,
look in **Workspace > Console** for setup instructions. If no dialog appears,
check `SlideShowCreator.log` beside the installed launcher for the traceback.
Some Resolve editions/versions restrict UI Manager or external scripting;
the command-line workflow remains available when UI Manager is unavailable.

## Command-line development

Try it against a running Resolve with an open project:

```
py -3.14 scripts\build_demo_timeline.py <folder-of-images> --transition slide_left
py -3.14 scripts\build_demo_timeline.py --list-transitions
```

For a read-only check that Resolve, a project, and a timeline are accessible,
run `py -3.14 scripts\live_check.py integration`. Other `live_check.py`
subcommands investigate Fusion behavior and may edit compositions or add them
to clips; run those only on a disposable timeline.

Still to come: auto-mix, per-clip motion (Ken Burns), bulk duration fitting,
titles, and soundtrack analysis / beat sync.

## Target environment

- DaVinci Resolve 20+ (free or Studio) on Windows / macOS / Linux
- A known-good Python 3.10 or 3.14 (64-bit) for the sidecar.
  **Avoid Python 3.12** on Windows: it crashes loading `fusionscript.dll`.
  Use `py -3.14 ...` (or `py -3.10 ...`) to invoke the helper scripts.
- Resolve must also discover a system Python 3.6+ to run the thin menu launcher.
  The launcher does not import `fusionscript` in that interpreter.
- UI Manager and external scripting API access in your Resolve edition/version.

## License

TBD
