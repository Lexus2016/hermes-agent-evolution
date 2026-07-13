"""Tests for config-drift validation in scripts.register_evolution_cron."""

import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "register_evolution_cron.py"


def _import_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("register_evolution_cron", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["register_evolution_cron"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestConfigDriftValidation:
    """Config-drift validation (#938): warn when agent-stage jobs have both
    model and provider unpinned. no_agent jobs are excluded."""

    def test_warns_when_unpinned(self, tmp_path, monkeypatch, capsys):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "analysis.yaml").write_text(
            "name: evolution-analysis\n"
            'schedule: "0 9 * * *"\n'
            "enabled: true\n"
            'prompt: "do analysis"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(mod, "_ensure_evolution_labels", lambda *a, **k: [])
        monkeypatch.setattr(mod, "_install_access_gate", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_install_evolution_helpers", lambda *a, **k: [])

        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod, "create_job", lambda **kw: {"id": "j1", "name": kw["name"]}
        )
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        captured = capsys.readouterr()
        assert "unpinned" in captured.err
        assert "evolution-analysis" in captured.err

    def test_silent_when_pinned_or_no_agent(self, tmp_path, monkeypatch, capsys):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "analysis.yaml").write_text(
            "name: evolution-analysis\n"
            'schedule: "0 9 * * *"\n'
            "enabled: true\n"
            'prompt: "do analysis"\n'
            "model: glm-5-flash\n"
            "provider: zai\n"
        )
        (src_dir / "funnel.yaml").write_text(
            "name: evolution-funnel\n"
            'schedule: "40 7 * * *"\n'
            "enabled: true\n"
            "no_agent: true\n"
            "script: evolution_funnel.py\n"
            'prompt: "deterministic script"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(mod, "_ensure_evolution_labels", lambda *a, **k: [])
        monkeypatch.setattr(mod, "_install_access_gate", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_install_evolution_helpers", lambda *a, **k: [])

        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod, "create_job", lambda **kw: {"id": "j1", "name": kw["name"]}
        )
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        captured = capsys.readouterr()
        assert "unpinned" not in captured.err

    def test_mixed_jobs(self, tmp_path, monkeypatch, capsys):
        mod = _import_module()
        src_dir = tmp_path / "cron-src"
        src_dir.mkdir()
        (src_dir / "research.yaml").write_text(
            "name: evolution-research\n"
            'schedule: "0 9 * * *"\n'
            "enabled: true\n"
            'prompt: "research"\n'
            "model: glm-5-flash\n"
            "provider: zai\n"
        )
        (src_dir / "analysis.yaml").write_text(
            "name: evolution-analysis\n"
            'schedule: "0 10 * * *"\n'
            "enabled: true\n"
            'prompt: "analysis"\n'
        )
        home = tmp_path / "hermes-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(mod, "_ensure_evolution_labels", lambda *a, **k: [])
        monkeypatch.setattr(mod, "_install_access_gate", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_install_evolution_helpers", lambda *a, **k: [])

        import cron.jobs as jobs_mod

        monkeypatch.setattr(
            jobs_mod, "create_job", lambda **kw: {"id": "j1", "name": kw["name"]}
        )
        monkeypatch.setattr(jobs_mod, "load_jobs", lambda: [])

        rc = mod.main(["register_evolution_cron.py", str(src_dir)])

        assert rc == 0
        captured = capsys.readouterr()
        assert "evolution-analysis" in captured.err
        assert "evolution-research" not in captured.err
