r"""Step-by-step diagnostic of the Resolve bridge.

Run this manually with Resolve open to pinpoint *exactly* which step crashes
or hangs. We do NOT use the resolve_bridge wrapper -- this is the lowest-level
test possible.

Usage:
    py -3.14 scripts\diagnose.py
    py -3.10 scripts\diagnose.py
"""

from __future__ import annotations

import os
import platform
import sys

print(f"[1] Python   : {sys.version}")
print(f"[1] Platform : {platform.platform()}")
print(f"[1] Arch     : {platform.machine()}")

api_dir = (
    os.environ.get("RESOLVE_SCRIPT_API")
    or r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
)
lib_path = (
    os.environ.get("RESOLVE_SCRIPT_LIB")
    or r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
)

print(f"[2] API dir  : {api_dir!r}  exists={os.path.isdir(api_dir)}")
print(f"[2] Lib file : {lib_path!r}  exists={os.path.isfile(lib_path)}")

os.environ["RESOLVE_SCRIPT_API"] = api_dir
os.environ["RESOLVE_SCRIPT_LIB"] = lib_path
sys.path.insert(0, os.path.join(api_dir, "Modules"))
print("[3] Env + sys.path configured.")
sys.stdout.flush()

print("[4] About to import DaVinciResolveScript ...")
sys.stdout.flush()
import DaVinciResolveScript as dvr  # noqa: E402
print(f"[4] Imported OK from: {dvr.__file__}")
sys.stdout.flush()

print("[5] About to call scriptapp('Resolve') ...")
sys.stdout.flush()
resolve = dvr.scriptapp("Resolve")
print(f"[5] scriptapp returned: {resolve!r}")
sys.stdout.flush()

if resolve is None:
    print("[6] resolve is None -- scripting likely disabled in Preferences,")
    print("    or fusionscript can't reach the Resolve process.")
    sys.exit(1)

print(f"[6] Product : {resolve.GetProductName()}")
print(f"[6] Version : {resolve.GetVersionString()}")
print(f"[6] Page    : {resolve.GetCurrentPage()}")
