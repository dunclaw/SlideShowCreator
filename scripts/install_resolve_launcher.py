"""Developer installation of the thin menu launcher; first-run wizard is issue #14."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def scripts_directory():
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / (
            "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility"
        )
    if sys.platform == "darwin":
        return Path.home() / (
            "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
        )
    return Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts/Utility"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-dir", type=Path, help="Override Resolve's per-user Utility folder")
    args = parser.parse_args(argv)
    if sys.prefix == sys.base_prefix:
        parser.error("Run this with the sidecar venv's Python, not the system Python. See README.")
    # Verify the interpreter can find the installed package even in isolated mode.
    subprocess.run(
        [sys.executable, "-I", "-c", "import slideshow.resolve_ui"],
        check=True,
    )
    destination = (args.scripts_dir or scripts_directory()).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    launcher = destination / "SlideShowCreator.py"
    source = Path(__file__).with_name("SlideShowCreator.py")
    config_path = launcher.with_suffix(".json")
    text = source.read_text(encoding="utf-8")
    placeholder = "\nCONFIG_PATH = None\n"
    if placeholder not in text:
        raise RuntimeError("Launcher template is missing its CONFIG_PATH placeholder.")
    text = text.replace(placeholder, "\nCONFIG_PATH = {0!r}\n".format(str(config_path)), 1)
    launcher.write_text(text, encoding="utf-8")
    config = {
        "python": sys.executable,
        "log": str(destination / "SlideShowCreator.log"),
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Installed {0}".format(launcher))
    print("Restart Resolve if needed, then choose Workspace > Scripts > SlideShowCreator.")
    print("If no dialog appears, check {0}".format(config["log"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
