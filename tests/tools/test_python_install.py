"""Tests for tools.python_install.py — PEP 668 transparent rewrite."""

import os
import pytest

from tools import python_install as pi


@pytest.fixture(autouse=True)
def reset_venv_cache(monkeypatch):
    """Each test starts with a clean venv cache and deterministic env."""
    pi._TRANSIENT_VENV_DIR = None
    yield
    pi._TRANSIENT_VENV_DIR = None


class TestIsPep668Active:
    def test_returns_bool(self, monkeypatch):
        """Smoke test: the function returns a boolean."""
        result = pi.is_pep668_active()
        assert isinstance(result, bool)

    def test_true_when_marker_exists(self, monkeypatch, tmp_path):
        fake_stdlib = str(tmp_path / "stdlib")
        marker = os.path.join(fake_stdlib, "EXTERNALLY-MANAGED")
        _real_join = os.path.join
        os.makedirs(fake_stdlib, exist_ok=True)
        open(marker, "w").close()
        orig_dirname = pi.os.path.dirname
        orig_join = pi.os.path.join
        orig_exists = pi.os.path.exists

        def _dirname(p):
            return fake_stdlib

        def _join(*a):
            return _real_join(*a)

        def _exists(p):
            return p == marker

        monkeypatch.setattr(pi.os.path, "dirname", _dirname)
        monkeypatch.setattr(pi.os.path, "join", _join)
        monkeypatch.setattr(pi.os.path, "exists", _exists)
        assert pi.is_pep668_active() is True

    def test_false_when_marker_missing(self, monkeypatch, tmp_path):
        fake_stdlib = str(tmp_path / "stdlib")
        os.makedirs(fake_stdlib, exist_ok=True)
        orig_dirname = pi.os.path.dirname

        def _dirname(p):
            return fake_stdlib

        def _exists(p):
            return False

        monkeypatch.setattr(pi.os.path, "dirname", _dirname)
        monkeypatch.setattr(pi.os.path, "exists", _exists)
        assert pi.is_pep668_active() is False


class TestRewritePipCommand:
    def test_noop_when_pep668_off(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: False)
        assert pi.rewrite_pip_command("pip install markdown") is None

    def test_noop_when_no_pip_install(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        assert pi.rewrite_pip_command("ls /tmp") is None

    def test_noop_when_break_system_packages(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        assert pi.rewrite_pip_command("pip install --break-system-packages markdown") is None

    def test_noop_when_user_flag(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        assert pi.rewrite_pip_command("pip install --user markdown") is None

    def test_rewrites_with_uv(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        monkeypatch.setattr(pi.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
        monkeypatch.setattr(pi, "_uv_python_flag", lambda: "--python=/usr/bin/python3")
        rewritten = pi.rewrite_pip_command("pip install markdown")
        assert rewritten is not None
        assert "uv pip install --python=/usr/bin/python3 markdown" in rewritten

    def test_rewrites_without_uv(self, monkeypatch, tmp_path):
        """When uv is missing, falls back to transient venv pip."""
        # _rewrite_to_transient_venv expects bin/pip under the returned venv dir
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_pip = fake_bin / "pip"
        fake_pip.touch()

        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        monkeypatch.setattr(pi.shutil, "which", lambda name: None)

        # Stub venv creation so it "succeeds" instantly
        def _stub_ensure():
            venv = str(tmp_path)
            pi._TRANSIENT_VENV_DIR = venv
            return venv, True
        monkeypatch.setattr(pi, "ensure_transient_venv", _stub_ensure)

        rewritten = pi.rewrite_pip_command("pip install markdown")
        assert rewritten is not None
        assert str(fake_pip) in rewritten

    def test_ignores_pip_uninstall(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        assert pi.rewrite_pip_command("pip uninstall markdown") is None

    def test_preserves_compound_command(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        monkeypatch.setattr(pi.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
        monkeypatch.setattr(pi, "_uv_python_flag", lambda: "--python=/usr/bin/python3")
        cmd = "cd /tmp && pip install markdown"
        rewritten = pi.rewrite_pip_command(cmd)
        assert rewritten is not None
        assert "uv pip install --python=/usr/bin/python3 markdown" in rewritten


class TestEnsureTransientVenv:
    def test_reuse_when_exists(self, tmp_path, monkeypatch):
        venv_dir = tmp_path / "venv"
        venv_dir.mkdir()
        (venv_dir / "bin").mkdir()
        (venv_dir / "bin" / "pip").touch()
        pi._TRANSIENT_VENV_DIR = str(venv_dir)
        path, created = pi.ensure_transient_venv()
        assert path == str(venv_dir)
        assert created is False

    def test_creates_when_missing(self, monkeypatch, tmp_path):
        """Actually creates a venv — slow but verifies the mechanism."""
        # Force a unique path via monkeypatch on gettempdir so we never clash
        monkeypatch.setattr(pi.tempfile, "gettempdir", lambda: str(tmp_path))
        pi._TRANSIENT_VENV_DIR = None
        # Force using a separate fake path instead of the marker-rewritten directory
        path, created = pi.ensure_transient_venv()
        assert created is True
        assert os.path.isdir(path)
        assert os.path.isfile(os.path.join(path, "bin", "pip"))


class TestGetInstallHint:
    def test_empty_when_pep668_off(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: False)
        assert pi.get_install_hint() == ""

    def test_hint_mentions_uv(self, monkeypatch):
        monkeypatch.setattr(pi, "is_pep668_active", lambda: True)
        monkeypatch.setattr(pi.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
        monkeypatch.setattr(pi, "_uv_python_flag", lambda: "--python=/usr/bin/python3")
        hint = pi.get_install_hint()
        assert "uv pip install" in hint
