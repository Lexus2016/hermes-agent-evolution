"""The committed prompt-surface snapshot is exactly what the code renders.

``tests/fixtures/prompt_surface/`` holds the system prompt and ``tools[]`` array a stock install
sends on each surface (see ``scripts/ci/prompt_surface.py``). A code change that alters either
must carry the regenerated snapshot, so the change shows up in review as real prompt text and
real schemas. CODEOWNERS identifies the reviewer; enforced approval depends on repository settings.

Regenerate with ``scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render``.
"""

from __future__ import annotations

import filecmp
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prompt_surface", REPO / "scripts" / "ci" / "prompt_surface.py")
assert _spec is not None and _spec.loader is not None
prompt_surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prompt_surface)


def _tree_differences(left: Path, right: Path) -> list[str]:
    cmp = filecmp.dircmp(left, right)
    out = [f"only in committed: {n}" for n in cmp.left_only] + [f"not committed: {n}" for n in cmp.right_only]
    _, mismatch, errors = filecmp.cmpfiles(left, right, cmp.common_files, shallow=False)
    out += [f"stale: {n}" for n in mismatch + errors]
    for sub in cmp.common_dirs:
        out += [f"{sub}/{d}" for d in _tree_differences(left / sub, right / sub)]
    return out


def test_committed_prompt_surface_is_current(tmp_path):
    rendered = tmp_path / "prompt_surface"
    assert prompt_surface.render(rendered) == 0
    differences = _tree_differences(prompt_surface.SNAPSHOT, rendered)
    assert not differences, (
        "The system prompt or tool schemas changed. Regenerate the snapshot and commit it with the "
        "change for owner review:\n"
        "    scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render\n"
        + "\n".join(differences[:20])
    )


def test_summary_names_the_surface_and_tool_that_changed(tmp_path):
    base = prompt_surface.SNAPSHOT
    head = tmp_path / "head"
    shutil.copytree(base, head)
    prompt = head / "prompts" / "cli.claude.txt"
    text = prompt.read_text(encoding="utf-8-sig")
    prompt.write_text(text.replace("# Finishing the job", "# Finish every job"), encoding="utf-8")
    tool = head / "tools" / "write_file.json"
    text = tool.read_text(encoding="utf-8-sig")
    tool.write_text(text.replace('"description": "', '"description": "Prefer patch. ', 1), encoding="utf-8")

    summary = prompt_surface.summarize(base, head)

    assert "| cli.claude |" in summary and "+# Finish every job" in summary
    assert "`write_file`: description +" in summary
    assert prompt_surface.summarize(base, base) == ""


def test_environment_paths_normalize_without_rewriting_real_guidance(tmp_path, monkeypatch):
    from agent import coding_context
    from tools import python_install

    monkeypatch.setattr(python_install, "is_pep668_active", lambda: True)
    monkeypatch.setattr(python_install, "has_uv", lambda: True)
    monkeypatch.setattr(python_install, "_uv_python_flag", lambda: f"--python={python_install.sys.executable}")
    # Match the marker-only fixture; unrelated host Git ancestors are not inputs.
    monkeypatch.setattr(coding_context, "_git_root", lambda _cwd: None)
    normalized = []
    for label in ("checkout-a", "checkout-b"):
        root = tmp_path / label
        home = root / "home"
        code = home / "code"
        code.mkdir(parents=True)
        (code / "pyproject.toml").write_text('[project]\nname = "fixture"\n', encoding="utf-8")
        executable = str(root / ".venv" / "bin" / "python")
        monkeypatch.setattr(prompt_surface, "ROOT", root)
        monkeypatch.setattr(python_install.sys, "executable", executable)
        hint = python_install.get_install_hint()
        assert f"--python={executable}" in hint  # Actual runtime guidance keeps the real interpreter.
        raw = hint + "\n" + coding_context.build_coding_workspace_block(code) + "\nReference: /opt/user-chosen/file"
        assert str(code) in raw
        scrubbed = prompt_surface._scrub(raw, home)
        assert executable not in scrubbed and str(root) not in scrubbed
        assert "--python=<PYTHON>" in scrubbed
        assert "/opt/user-chosen/file" in scrubbed  # Never hide arbitrary prompt paths.
        normalized.append(scrubbed)
    assert normalized[0] == normalized[1]


def test_full_render_is_identical_under_distinct_temp_roots(tmp_path, monkeypatch):
    outputs = []
    for label in ("temp-a", "temp-b"):
        temp_root = tmp_path / label
        temp_root.mkdir()
        monkeypatch.setattr(prompt_surface.tempfile, "tempdir", str(temp_root))
        output = tmp_path / (label + "-output")
        assert prompt_surface.render(output) == 0
        outputs.append(output)
    assert not _tree_differences(*outputs)
    plain = (outputs[0] / "prompts" / "cli.claude.txt").read_text(encoding="utf-8-sig")
    code = (outputs[0] / "prompts" / "cli-code.claude.txt").read_text(encoding="utf-8-sig")
    assert "- Root: <HOME>/code" in code
    assert "You are a coding agent pairing" in code
    assert "Workspace (snapshot" not in plain
    assert "You are a coding agent pairing" not in plain


@pytest.mark.platforms("posix")
def test_full_render_ignores_host_acp_availability(tmp_path, monkeypatch):
    from tools.delegate_tool import _acp_binary_available

    absent = tmp_path / "no-acp"
    present = tmp_path / "with-acp"
    absent.mkdir()
    present.mkdir()
    invoked = tmp_path / "acp-invoked"
    executable = present / "codex"
    executable.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"Path({str(invoked)!r}).touch()\nraise SystemExit(99)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    outputs = []
    for path, available in ((absent, False), (present, True)):
        monkeypatch.setenv("PATH", str(path))
        assert _acp_binary_available() is available
        output = tmp_path / f"{path.name}-output"
        assert prompt_surface.render(output) == 0
        outputs.append(output)

    assert not _tree_differences(*outputs)
    assert not invoked.exists()
