"""Regression for #166: the sensitive-path write guard must reject real system
paths but ALLOW the OS temp dir (macOS temp lives under /var/folders ->
/private/var/folders, which the /private/var/ prefix would otherwise reject)."""

import os
import tempfile

from tools.file_tools import _check_sensitive_path


def test_os_temp_dir_is_allowed():
    p = os.path.join(tempfile.gettempdir(), "hermes_guard_probe.txt")
    assert _check_sensitive_path(p) is None


def test_macos_var_folders_is_allowed():
    # The canonical macOS per-user temp root, both pre- and post-realpath forms.
    assert _check_sensitive_path("/var/folders/jz/abc/T/x.txt") is None
    assert _check_sensitive_path("/private/var/folders/jz/abc/T/x.txt") is None


def test_real_system_paths_still_blocked():
    for p in ("/etc/passwd", "/private/etc/hosts", "/private/var/db/secret"):
        assert _check_sensitive_path(p) is not None, f"{p} must stay blocked"


def test_hermes_config_inside_temp_still_blocked(monkeypatch):
    # The temp exemption must NOT bypass the Hermes-config guard: a config file
    # that resolves inside a temp dir (as in the test suite) must still be
    # refused. Regression for the over-broad first cut of this fix.
    import os
    import tempfile

    import tools.file_tools as ft

    cfg = os.path.realpath(os.path.join(tempfile.gettempdir(), "cfgguard", "config.yaml"))
    monkeypatch.setattr(ft, "_hermes_config_resolved", cfg)
    monkeypatch.setattr(ft, "_hermes_config_resolved_loaded", True)
    assert _check_sensitive_path(cfg) is not None
