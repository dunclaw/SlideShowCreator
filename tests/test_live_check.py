"""CLI tests for the live Resolve check script."""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_check.py"
SPEC = importlib.util.spec_from_file_location("live_check", SCRIPT_PATH)
live_check = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(live_check)


def test_parser_exposes_all_live_check_commands():
    parser = live_check.build_parser()
    for command in ("integration", "active", "keyframes", "point-v3", "target", "modified"):
        args = parser.parse_args([command])
        assert args.command == command
        assert callable(args.run)


def test_clip_commands_default_to_first_clip():
    parser = live_check.build_parser()
    for command in ("active", "keyframes"):
        args = parser.parse_args([command])
        assert (args.track, args.index) == (1, 1)


def test_integration_check_reports_live_project_and_timeline(monkeypatch, capsys):
    context = MagicMock()
    context.resolve.GetProductName.return_value = "Resolve"
    context.resolve.GetVersionString.return_value = "20"
    context.project.GetName.return_value = "Demo"
    context.current_timeline.GetName.return_value = "Timeline"
    context.current_timeline.GetTrackCount.side_effect = [1, 0]
    context.current_timeline.GetItemListInTrack.return_value = ["clip"]
    monkeypatch.setattr(live_check, "ResolveContext", lambda: context)

    assert live_check.integration_check(Namespace()) == 0

    output = capsys.readouterr().out
    assert "Project: 'Demo'" in output
    assert "Timeline: 'Timeline'" in output
    assert "PASS:" in output
