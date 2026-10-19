"""Resolve menu entry: stdlib only, compatible with the host's Python 3.6+."""

import json
from pathlib import Path
import subprocess
import sys
import os

# Resolve runs menu scripts with __file__ = None, so the installer writes this path.
CONFIG_PATH = None


def main():
    config_path = Path(
        CONFIG_PATH or Path(globals().get("__file__") or "SlideShowCreator.py").with_suffix(".json")
    )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        python = config["python"]
        log = Path(config["log"])
        if not Path(python).is_file():
            raise ValueError("Sidecar Python not found: {0}".format(python))
        log.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        with log.open("a", encoding="utf-8") as output:
            subprocess.Popen(
                [python, "-I", "-u", "-m", "slideshow.resolve_ui"],
                stdout=output, stderr=subprocess.STDOUT, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        print("SlideShowCreator started. If no dialog appears, see: {0}".format(log))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        message = (
            "SlideShowCreator could not start: {0}\n"
            "Create the sidecar venv and run scripts\\install_resolve_launcher.py "
            "with its Python (see README). Expected configuration: {1}"
        ).format(exc, config_path)
        print(message, file=sys.stderr)
        # Resolve injects these handles; do not import its SDK into the host Python.
        app = globals().get("fusion") or globals().get("fu")
        bmd = globals().get("bmd")
        if app is not None and bmd is not None:
            ui = app.UIManager
            dispatcher = bmd.UIDispatcher(ui)
            window = dispatcher.AddWindow(
                {"ID": "SSCError", "WindowTitle": "SlideShowCreator setup required",
                 "Geometry": [200, 200, 650, 200]},
                ui.VGroup([ui.TextEdit({"ReadOnly": True, "PlainText": message})]),
            )
            window.On.SSCError.Close = lambda event: dispatcher.ExitLoop()
            window.Show()
            try:
                dispatcher.RunLoop()
            finally:
                window.Hide()


if __name__ == "__main__":
    main()
