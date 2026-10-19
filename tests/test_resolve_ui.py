"""UI and menu handoff coverage without a Resolve installation."""

import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from slideshow import resolve_ui
from slideshow.folder_project import gather_images, project_from_folder
from slideshow.project_model import SlideshowProject


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def images(tmp_path):
    for name in ("b.PNG", "a.jpg", "notes.txt"):
        (tmp_path / name).touch()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "hidden.jpg").touch()
    return tmp_path


def test_folder_project_uses_same_alphabetical_order_as_demo(images):
    assert [Path(path).name for path in gather_images(images)] == ["a.jpg", "b.PNG"]
    project = project_from_folder(str(images), "2.5", "auto", "  Family  ")
    assert project.name == "Family"
    assert project.default_item_duration_seconds == 2.5
    assert project.default_transition.kind == "auto"
    assert all(Path(item.path).is_absolute() for item in project.items)


def test_folder_order_preserves_demo_case_sensitive_sort(images):
    (images / "C.jpg").touch()
    assert [Path(path).name for path in gather_images(images)] == ["C.jpg", "a.jpg", "b.PNG"]


@pytest.mark.parametrize("seconds", ["", "bad", "0", "-1", "nan", "inf", "-inf"])
def test_invalid_duration_is_rejected(images, seconds):
    with pytest.raises(ValueError, match="seconds"):
        project_from_folder(str(images), seconds, "dissolve", "Slideshow")


@pytest.mark.parametrize(
    "folder,transition,name,match",
    [
        ("", "dissolve", "Slides", "Choose a folder"),
        ("missing", "dissolve", "Slides", "Not an image directory"),
        ("", "bogus", "Slides", "Unknown transition"),
        ("", "dissolve", "  ", "timeline name"),
    ],
)
def test_invalid_inputs_are_rejected(folder, transition, name, match):
    with pytest.raises(ValueError, match=match):
        project_from_folder(folder, 4, transition, name)


def test_empty_folder_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="No supported images"):
        project_from_folder(str(tmp_path), 4, "none", "Slides")


class FakeUI:
    def __getattr__(self, kind):
        def create(value):
            return (kind, value)
        return create


class FakeCombo(SimpleNamespace):
    def AddItems(self, values):
        self.values = values


class FakeDispatcher:
    def __init__(self):
        self.items = {}
        self.exited = False
        self.visible = False

    def AddWindow(self, attributes, layout):
        def collect(node):
            kind, value = node
            if isinstance(value, list):
                for child in value:
                    collect(child)
            elif "ID" in value:
                item = FakeCombo(**value) if kind == "ComboBox" else SimpleNamespace(**value)
                if kind == "LineEdit" and not hasattr(item, "Text"):
                    item.Text = ""
                item.Enabled = True
                self.items[value["ID"]] = item
        collect(layout)
        self.window = SimpleNamespace(
            On=SimpleNamespace(**{
                key: SimpleNamespace()
                for key in ("SlideShowCreator", "Close", "Browse", "Build")
            }),
            GetItems=lambda: self.items,
            Show=lambda: setattr(self, "visible", True),
            Hide=lambda: setattr(self, "visible", False),
        )
        return self.window

    def ExitLoop(self):
        self.exited = True

    def RunLoop(self):
        assert self.visible
        self.window.On.Close.Clicked({})


@pytest.fixture
def dialog(images, monkeypatch, tmp_path):
    context = SimpleNamespace(
        resolve=object(),
        project=SimpleNamespace(GetName=lambda: "Open project"),
        fusion=SimpleNamespace(RequestDir=lambda initial: str(images)),
    )
    monkeypatch.setattr(resolve_ui, "project_directory", lambda: tmp_path / "records")
    dispatcher = FakeDispatcher()
    dialog = resolve_ui.SlideshowDialog(context, FakeUI(), dispatcher)
    dialog.browse({})
    return dialog


def test_build_saves_json_before_using_existing_builder(dialog, monkeypatch, tmp_path):
    def build(project, context):
        records = list((tmp_path / "records").glob("*/project.json"))
        assert len(records) == 1
        saved = SlideshowProject.load_from_file(str(records[0]))
        assert saved.to_dict() == project.to_dict()
        assert context.resolve is dialog.context.resolve
        assert not dialog.items["Build"].Enabled
        return SimpleNamespace(timeline=SimpleNamespace(GetName=lambda: project.name))

    monkeypatch.setattr(resolve_ui, "build_slideshow", build)
    dialog.window.On.Build.Clicked({})
    assert "Created timeline 'Slideshow'" in dialog.items["Status"].PlainText
    assert "Build record:" in dialog.items["Status"].PlainText
    assert dialog.items["Build"].Enabled
    assert dialog.items["Browse"].Enabled
    assert dialog.items["Close"].Enabled


def test_invalid_input_does_not_build_or_write_records(dialog, monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must not reach Resolve")

    monkeypatch.setattr(resolve_ui, "build_slideshow", unexpected)
    dialog.items["Seconds"].Text = "nan"
    dialog.build({})
    assert "finite, positive" in dialog.items["Status"].PlainText
    assert not (tmp_path / "records").exists()
    assert dialog.items["Build"].Enabled


def test_build_failure_is_visible_and_retry_is_enabled(dialog, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Import failed")

    monkeypatch.setattr(resolve_ui, "build_slideshow", fail)
    dialog.build({})
    assert "Import failed" in dialog.items["Status"].PlainText
    assert "partial timeline" in dialog.items["Status"].PlainText
    assert dialog.items["Build"].Enabled


def test_no_open_project_does_not_write_or_build(dialog, monkeypatch, tmp_path):
    class MissingProject:
        @property
        def project(self):
            raise RuntimeError("No project is open in DaVinci Resolve.")

    dialog.context = MissingProject()
    dialog.build({})
    assert "No project is open" in dialog.items["Status"].PlainText
    assert not (tmp_path / "records").exists()
    assert dialog.items["Build"].Enabled


def test_folder_picker_cancel_keeps_previous_folder(dialog):
    previous = dialog.items["Folder"].Text
    dialog.context.fusion.RequestDir = lambda initial: None
    dialog.browse({})
    assert dialog.items["Folder"].Text == previous


def test_close_exits_and_hides_window(dialog):
    dialog.run()
    assert dialog.dispatcher.exited
    assert not dialog.dispatcher.visible


def load_launcher(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts" / "SlideShowCreator.py"))
    main = module["main"]
    main.__globals__["__file__"] = str(tmp_path / "SlideShowCreator.py")
    return main


def test_launcher_missing_setup_is_actionable(tmp_path, capsys):
    load_launcher(tmp_path)()
    error = capsys.readouterr().err
    assert "could not start" in error
    assert "install_resolve_launcher.py" in error
    assert str(tmp_path / "SlideShowCreator.json") in error


def test_launcher_hands_off_to_pinned_python_without_host_environment(tmp_path, monkeypatch):
    main = load_launcher(tmp_path)
    python = tmp_path / "sidecar with spaces" / "python.exe"
    python.parent.mkdir()
    python.touch()
    log = tmp_path / "logs" / "sidecar.log"
    (tmp_path / "SlideShowCreator.json").write_text(
        json.dumps({"python": str(python), "log": str(log)}), encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONHOME", "host-python")
    monkeypatch.setenv("PYTHONPATH", "host-modules")
    calls = []
    monkeypatch.setattr(main.__globals__["subprocess"], "Popen", lambda *a, **kw: calls.append((a, kw)))
    main()
    args, kwargs = calls[0]
    assert args[0] == [str(python), "-I", "-u", "-m", "slideshow.resolve_ui"]
    assert "PYTHONHOME" not in kwargs["env"]
    assert "PYTHONPATH" not in kwargs["env"]
    assert not kwargs.get("shell")
    assert log.exists()


def test_launcher_deleted_venv_is_actionable(tmp_path, capsys):
    (tmp_path / "SlideShowCreator.json").write_text(
        json.dumps({"python": str(tmp_path / "missing.exe"), "log": str(tmp_path / "log")}),
        encoding="utf-8",
    )
    load_launcher(tmp_path)()
    assert "Sidecar Python not found" in capsys.readouterr().err


def test_launcher_malformed_config_is_actionable(tmp_path, capsys):
    (tmp_path / "SlideShowCreator.json").write_text("{bad json", encoding="utf-8")
    load_launcher(tmp_path)()
    assert "install_resolve_launcher.py" in capsys.readouterr().err


def test_installer_copies_thin_launcher_and_records_interpreter(tmp_path, monkeypatch):
    installer = runpy.run_path(str(ROOT / "scripts" / "install_resolve_launcher.py"))
    main = installer["main"]
    monkeypatch.setattr(main.__globals__["sys"], "prefix", "sidecar-venv")
    monkeypatch.setattr(main.__globals__["sys"], "base_prefix", "system-python")
    calls = []
    monkeypatch.setattr(
        main.__globals__["subprocess"], "run", lambda *a, **kw: calls.append((a, kw))
    )
    assert main(["--scripts-dir", str(tmp_path)]) == 0
    config = json.loads((tmp_path / "SlideShowCreator.json").read_text(encoding="utf-8"))
    assert config["python"] == main.__globals__["sys"].executable
    assert calls[0][0][0][-1] == "import slideshow.resolve_ui"

    # Resolve's fuscript host runs menu scripts with __file__ = None.
    installed = runpy.run_path(str(tmp_path / "SlideShowCreator.py"))
    assert installed["CONFIG_PATH"] == str(tmp_path / "SlideShowCreator.json")
    launched = []
    launch = installed["main"]
    launch.__globals__["__file__"] = None
    monkeypatch.setattr(
        launch.__globals__["subprocess"], "Popen", lambda *a, **kw: launched.append(a)
    )
    launch()
    assert launched[0][0][0] == config["python"]


def test_installer_rejects_system_python(monkeypatch):
    installer = runpy.run_path(str(ROOT / "scripts" / "install_resolve_launcher.py"))
    monkeypatch.setattr(installer["main"].__globals__["sys"], "prefix", "system")
    monkeypatch.setattr(installer["main"].__globals__["sys"], "base_prefix", "system")
    with pytest.raises(SystemExit, match="2"):
        installer["main"]([])
