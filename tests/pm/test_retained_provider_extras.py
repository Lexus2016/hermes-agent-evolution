"""Fork provider dependency activation uses PM and refuses unsupported native extras."""
import pytest

from pm import extras


@pytest.mark.parametrize(("extra", "anchor"), [("honcho", "honcho"), ("mem0", "mem0"),
                                                 ("homeassistant", "aiohttp"), ("otel", "opentelemetry.sdk")])
def test_retained_provider_extra_has_available_anchor_without_install(extra, anchor, monkeypatch):
    synced = []
    monkeypatch.setattr(extras, "_importable", lambda module: module == anchor)
    from pm import client
    monkeypatch.setattr(client, "sync_venv", lambda *args, **kwargs: synced.append(args))
    extras.ensure_import(extra)
    assert synced == []


def test_google_meet_android_gate_preserves_supported_desktop_targets(monkeypatch):
    monkeypatch.setattr(extras, "_PLATFORM_GATES", None)
    for system in ("linux", "darwin", "win32", "android"):
        assert extras.extra_supported(
            "google-meet", environment={"sys_platform": system}, importable=lambda _: False,
        ) is (system != "android")
