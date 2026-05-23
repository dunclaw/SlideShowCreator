"""Resolve bridge — locate the scripting SDK, import it, and connect to Resolve.

This module is the single entry point any other part of the plug-in uses to talk
to DaVinci Resolve. It can run in two contexts:

1. **Inside Resolve** (script invoked via Workspace → Scripts).
   ``DaVinciResolveScript`` is already importable; environment is preconfigured.

2. **Outside Resolve** (development / smoke tests / sidecar processes).
   The SDK location is not on ``sys.path``; we add it ourselves using the
   well-known install paths (or honor ``RESOLVE_SCRIPT_API`` if set).

The bridge avoids any non-stdlib imports so it remains compatible with
Resolve's bundled Python interpreter.
"""

from __future__ import annotations

import importlib
import os
import platform
import subprocess
import sys
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# SDK location
# --------------------------------------------------------------------------- #

def _default_api_dir() -> Optional[str]:
    system = platform.system()
    if system == "Windows":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return os.path.join(
            program_data,
            "Blackmagic Design",
            "DaVinci Resolve",
            "Support",
            "Developer",
            "Scripting",
        )
    if system == "Darwin":
        return (
            "/Library/Application Support/Blackmagic Design/"
            "DaVinci Resolve/Developer/Scripting"
        )
    if system == "Linux":
        return "/opt/resolve/Developer/Scripting"
    return None


def _default_lib_path() -> Optional[str]:
    system = platform.system()
    if system == "Windows":
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        return os.path.join(
            program_files,
            "Blackmagic Design",
            "DaVinci Resolve",
            "fusionscript.dll",
        )
    if system == "Darwin":
        return (
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/"
            "Contents/Libraries/Fusion/fusionscript.so"
        )
    if system == "Linux":
        return "/opt/resolve/libs/Fusion/fusionscript.so"
    return None


def configure_sdk_paths() -> str:
    """Ensure the Resolve scripting SDK is importable from this interpreter.

    Honors ``RESOLVE_SCRIPT_API`` and ``RESOLVE_SCRIPT_LIB`` if set; otherwise
    falls back to the platform's default install location.

    Returns the API directory it configured. Raises ``RuntimeError`` if the
    SDK cannot be found on disk.
    """
    api_dir = os.environ.get("RESOLVE_SCRIPT_API") or _default_api_dir()
    lib_path = os.environ.get("RESOLVE_SCRIPT_LIB") or _default_lib_path()

    if not api_dir or not os.path.isdir(api_dir):
        raise RuntimeError(
            "Resolve scripting SDK directory not found. Looked at: {0!r}. "
            "Set RESOLVE_SCRIPT_API to override.".format(api_dir)
        )
    if not lib_path or not os.path.isfile(lib_path):
        raise RuntimeError(
            "Resolve scripting library not found. Looked at: {0!r}. "
            "Set RESOLVE_SCRIPT_LIB to override.".format(lib_path)
        )

    os.environ["RESOLVE_SCRIPT_API"] = api_dir
    os.environ["RESOLVE_SCRIPT_LIB"] = lib_path

    modules_dir = os.path.join(api_dir, "Modules")
    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)

    return api_dir


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

class ResolveNotRunningError(RuntimeError):
    """Raised when DaVinci Resolve is not running or scripting access is blocked."""


def is_resolve_running() -> bool:
    """Best-effort check that a DaVinci Resolve process is alive.

    Calling ``DaVinciResolveScript.scriptapp("Resolve")`` against ``fusionscript``
    when no Resolve process is running can crash the host interpreter (the
    library segfaults rather than returning ``None``). This guard lets us turn
    that into a clean Python exception.
    """
    # When loaded inside Resolve itself, the SDK module is preloaded; trust it.
    if "DaVinciResolveScript" in sys.modules:
        return True

    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH", "/FO", "CSV"],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).decode("utf-8", "replace")
            return "Resolve.exe" in out
        # macOS / Linux
        out = subprocess.check_output(
            ["pgrep", "-x", "Resolve"], stderr=subprocess.DEVNULL
        ).decode("utf-8", "replace")
        return bool(out.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def get_resolve() -> Any:
    """Return the top-level ``Resolve`` scripting object, or raise."""
    configure_sdk_paths()

    if not is_resolve_running():
        raise ResolveNotRunningError(
            "DaVinci Resolve process not detected. Start Resolve before "
            "running this script (calling into fusionscript without a host "
            "Resolve process can crash the interpreter)."
        )

    try:
        dvr = importlib.import_module("DaVinciResolveScript")
    except Exception as exc:  # pragma: no cover - depends on SDK on disk
        raise RuntimeError(
            "Failed to import DaVinciResolveScript: {0}".format(exc)
        ) from exc

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveNotRunningError(
            "Resolve scriptapp() returned None. Is scripting enabled "
            "(Preferences → System → General → External scripting using = Local)?"
        )
    return resolve


# --------------------------------------------------------------------------- #
# Convenience accessors
# --------------------------------------------------------------------------- #

class ResolveContext:
    """Lazy-resolved handles to the objects every feature module needs."""

    def __init__(self, resolve: Optional[Any] = None) -> None:
        self._resolve = resolve

    @property
    def resolve(self) -> Any:
        if self._resolve is None:
            self._resolve = get_resolve()
        return self._resolve

    @property
    def fusion(self) -> Any:
        return self.resolve.Fusion()

    @property
    def project_manager(self) -> Any:
        return self.resolve.GetProjectManager()

    @property
    def project(self) -> Any:
        proj = self.project_manager.GetCurrentProject()
        if proj is None:
            raise RuntimeError("No project is open in DaVinci Resolve.")
        return proj

    @property
    def media_pool(self) -> Any:
        return self.project.GetMediaPool()

    @property
    def media_storage(self) -> Any:
        return self.resolve.GetMediaStorage()

    @property
    def current_timeline(self) -> Optional[Any]:
        return self.project.GetCurrentTimeline()


# --------------------------------------------------------------------------- #
# CLI smoke test
# --------------------------------------------------------------------------- #

def _smoke_test() -> int:
    """Print a few facts about the running Resolve instance. Returns exit code."""
    try:
        api_dir = configure_sdk_paths()
        print("SDK dir : {0}".format(api_dir))
    except RuntimeError as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 2

    try:
        ctx = ResolveContext()
        r = ctx.resolve
    except ResolveNotRunningError as exc:
        print("FAIL: {0}".format(exc), file=sys.stderr)
        return 3
    except Exception as exc:  # pragma: no cover
        print("FAIL: unexpected error: {0!r}".format(exc), file=sys.stderr)
        return 4

    print("Resolve : {0}".format(r.GetProductName()))
    print("Version : {0}".format(r.GetVersionString()))
    print("Page    : {0}".format(r.GetCurrentPage()))

    pm = ctx.project_manager
    proj = pm.GetCurrentProject()
    if proj is None:
        print("Project : <none open>")
        return 0

    print("Project : {0}".format(proj.GetName()))
    print("Timelines: {0}".format(proj.GetTimelineCount()))
    tl = proj.GetCurrentTimeline()
    if tl is not None:
        print(
            "Current timeline: {0!r} ({1} video, {2} audio tracks)".format(
                tl.GetName(),
                tl.GetTrackCount("video"),
                tl.GetTrackCount("audio"),
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_smoke_test())
