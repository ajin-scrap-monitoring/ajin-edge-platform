import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest


@pytest.fixture
def checker(monkeypatch):
    tools = Path(__file__).resolve().parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    spec = importlib.util.spec_from_file_location(
        "check_package_private", tools / "check_package_private.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    monkeypatch.setenv("GH_TOKEN", "test-only")
    monkeypatch.setattr(sys, "argv", ["check_package_private.py", "camera-edge"])
    return loaded


@pytest.mark.parametrize("visibility", ["public", "internal", None])
def test_rejects_non_private_package(checker, monkeypatch, visibility):
    monkeypatch.setattr(
        checker.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps({"visibility": visibility}).encode()),
    )
    with pytest.raises(SystemExit, match="not private"):
        checker.main()


def test_accepts_verified_private_package(checker, monkeypatch):
    monkeypatch.setattr(
        checker.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b'{"visibility":"private"}')
    )
    checker.main()


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_http_failures_fail_closed(checker, monkeypatch, code):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", code, "test", {}, None)

    monkeypatch.setattr(checker.urllib.request, "urlopen", fail)
    with pytest.raises(SystemExit, match="cannot verify"):
        checker.main()
