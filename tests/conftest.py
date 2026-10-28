"""Shared test isolation: never touch the real desktop or taskbar shortcuts."""

import pytest

import tbrain.installer as installer


@pytest.fixture(autouse=True)
def _isolate_shortcuts(monkeypatch):
    """Keep every install/switch test away from the user's real shortcuts."""
    original = installer.retarget_shortcuts
    monkeypatch.setattr(installer, "retarget_shortcuts",
                        lambda *args, **kwargs: {"ok": True, "changed": [], "skipped": "test"})
    return original
