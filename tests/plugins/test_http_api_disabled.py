from __future__ import annotations

import importlib.util
from pathlib import Path

from toolsets import TOOLSETS, resolve_toolset


PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "http-api"


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_test_http_api_disabled",
        PLUGIN_ROOT / "__init__.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_toolset_does_not_expose_call_http_api() -> None:
    assert "call_http_api" not in TOOLSETS["runtime"]["tools"]
    assert "call_http_api" not in resolve_toolset("runtime")


def test_http_api_plugin_does_not_register_a_model_tool() -> None:
    registered = []

    class FakeContext:
        def register_tool(self, **kwargs):
            registered.append(kwargs)

    module = _load_plugin_module()
    module.register(FakeContext())

    assert registered == []
