"""Native Resolve UI, hosted by the external sidecar (never imported by the launcher)."""

import os
from pathlib import Path
import sys
import traceback
import uuid

from .folder_project import project_from_folder
from .resolve_bridge import ResolveContext
from .timeline_builder import build_slideshow
from .transitions import registered_kinds


def project_directory():
    """User-writable build records, separate from source and the image folder."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "SlideShowCreator" / "projects"


class SlideshowDialog:
    def __init__(self, context, ui, dispatcher):
        self.context = context
        self.dispatcher = dispatcher
        self.transition_kinds = ["auto", "none"] + list(registered_kinds())
        self.window = dispatcher.AddWindow(
            {
                "ID": "SlideShowCreator",
                "WindowTitle": "SlideShowCreator",
                "Geometry": [200, 200, 640, 330],
            },
            ui.VGroup([
                ui.HGroup([
                    ui.Label({"Text": "Image folder", "Weight": 0}),
                    ui.LineEdit({"ID": "Folder", "PlaceholderText": "Choose a folder of images"}),
                    ui.Button({"ID": "Browse", "Text": "Browse...", "Weight": 0}),
                ]),
                ui.HGroup([
                    ui.Label({"Text": "Seconds per slide", "Weight": 0}),
                    ui.LineEdit({"ID": "Seconds", "Text": "4"}),
                    ui.Label({"Text": "Transition", "Weight": 0}),
                    ui.ComboBox({"ID": "Transition"}),
                ]),
                ui.HGroup([
                    ui.Label({"Text": "Timeline name", "Weight": 0}),
                    ui.LineEdit({"ID": "Name", "Text": "Slideshow"}),
                ]),
                ui.Label({
                    "Text": "Images are sorted alphabetically (no subfolders). "
                            "Auto currently uses dissolve.",
                    "WordWrap": True,
                    "Weight": 0,
                }),
                ui.TextEdit({"ID": "Status", "ReadOnly": True, "PlainText": "Ready."}),
                ui.HGroup([
                    ui.Button({"ID": "Build", "Text": "Create timeline"}),
                    ui.Button({"ID": "Close", "Text": "Close"}),
                ]),
            ]),
        )
        if self.window is None:
            raise RuntimeError("Resolve UI Manager could not create the SlideShowCreator dialog.")
        self.items = self.window.GetItems()
        self.items["Transition"].AddItems(
            ["Auto (currently dissolve)", "None (hard cuts)"] + list(registered_kinds())
        )
        self.items["Transition"].CurrentIndex = self.transition_kinds.index("dissolve")
        self.window.On.SlideShowCreator.Close = self.close
        self.window.On.Close.Clicked = self.close
        self.window.On.Browse.Clicked = self.browse
        self.window.On.Build.Clicked = self.build

    def close(self, event):
        self.dispatcher.ExitLoop()

    def browse(self, event):
        try:
            folder = self.context.fusion.RequestDir(self.items["Folder"].Text or "")
            if folder:
                self.items["Folder"].Text = folder
        except Exception as exc:
            traceback.print_exc()
            self.items["Status"].PlainText = "Could not open folder picker: {0}".format(exc)

    def build(self, event):
        for key in ("Build", "Browse", "Close"):
            self.items[key].Enabled = False
        started_build = False
        saved_path = None
        try:
            project = project_from_folder(
                self.items["Folder"].Text,
                self.items["Seconds"].Text,
                self.transition_kinds[self.items["Transition"].CurrentIndex],
                self.items["Name"].Text,
            )
            # Resolve may have changed projects while this dialog was open.
            resolve_project = self.context.project
            path = project_directory() / uuid.uuid4().hex / "project.json"
            path.parent.mkdir(parents=True)
            project.save_to_file(str(path))
            saved_path = path
            self.items["Status"].PlainText = "Building {0} slides...".format(len(project.items))
            started_build = True
            result = build_slideshow(project, context=ResolveContext(self.context.resolve))
            self.items["Status"].PlainText = (
                "Created timeline {0!r} in project {1!r} from {2} images.\n"
                "Build record: {3}".format(
                    result.timeline.GetName(), resolve_project.GetName(), len(project.items), path
                )
            )
        except Exception as exc:
            traceback.print_exc()
            message = "Could not create timeline: {0}".format(exc)
            if started_build:
                message += "\nResolve may contain a partial timeline; inspect it before retrying."
            if saved_path:
                message += "\nBuild record: {0}".format(saved_path)
            self.items["Status"].PlainText = message
        finally:
            for key in ("Build", "Browse", "Close"):
                self.items[key].Enabled = True

    def run(self):
        self.window.Show()
        try:
            self.dispatcher.RunLoop()
        finally:
            self.window.Hide()


def main():
    try:
        context = ResolveContext()
        fusion = context.fusion
        ui = fusion.UIManager
        if ui is None:
            raise RuntimeError(
                "UI Manager is unavailable. This Resolve edition/version may not support it; "
                "try Resolve Studio or use scripts\\build_demo_timeline.py."
            )
        import DaVinciResolveScript as bmd

        dispatcher = bmd.UIDispatcher(ui)
        SlideshowDialog(context, ui, dispatcher).run()
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
