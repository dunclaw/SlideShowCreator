"""Unit tests for resolve_bridge (no Resolve required).

These cover the path-resolution / env-var plumbing that runs before any actual
Resolve connection. The connection itself requires a running Resolve and is
exercised by scripts/smoke_test.py.
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure src/ is importable when running pytest from project root.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from slideshow import resolve_bridge  # noqa: E402


def test_default_api_dir_is_platform_appropriate():
    api = resolve_bridge._default_api_dir()
    lib = resolve_bridge._default_lib_path()
    assert api is not None
    assert lib is not None
    # Loose sanity: both should mention 'Resolve' on every supported OS.
    assert "Resolve" in api
    assert "Resolve" in lib or lib.endswith(("fusionscript.so", "fusionscript.dll"))


def test_configure_sdk_paths_raises_when_missing(tmp_path, monkeypatch):
    bogus = tmp_path / "does-not-exist"
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(bogus))
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(bogus / "fake.dll"))
    with pytest.raises(RuntimeError, match="SDK directory not found"):
        resolve_bridge.configure_sdk_paths()


def test_configure_sdk_paths_accepts_env_override(tmp_path, monkeypatch):
    api_dir = tmp_path / "api"
    modules_dir = api_dir / "Modules"
    modules_dir.mkdir(parents=True)
    lib_file = tmp_path / "fusionscript.dll"
    lib_file.write_bytes(b"")

    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(api_dir))
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(lib_file))

    # Snapshot sys.path so we can verify the Modules dir is added.
    original_path = list(sys.path)
    try:
        result = resolve_bridge.configure_sdk_paths()
        assert result == str(api_dir)
        assert str(modules_dir) in sys.path
    finally:
        sys.path[:] = original_path


def test_is_resolve_running_returns_bool():
    # Should never raise, regardless of whether Resolve is actually running.
    assert isinstance(resolve_bridge.is_resolve_running(), bool)


def test_get_resolve_raises_clean_error_when_not_running(tmp_path, monkeypatch):
    api_dir = tmp_path / "api"
    (api_dir / "Modules").mkdir(parents=True)
    lib_file = tmp_path / "fusionscript.dll"
    lib_file.write_bytes(b"")
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(api_dir))
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(lib_file))

    monkeypatch.setattr(resolve_bridge, "is_resolve_running", lambda: False)
    # Defend against a previously-cached import of the real SDK module.
    monkeypatch.delitem(sys.modules, "DaVinciResolveScript", raising=False)

    with pytest.raises(resolve_bridge.ResolveNotRunningError):
        resolve_bridge.get_resolve()
