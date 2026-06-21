from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from vuln_verify.launcher import launch
from vuln_verify.prompt import load_prompt


@pytest.fixture
def mock_assembled_dir(tmp_path: Path) -> Path:
    assembled = tmp_path / "assembled"
    (assembled / "groups" / "group_001" / "reports").mkdir(parents=True)
    (assembled / "groups" / "group_002" / "reports").mkdir(parents=True)
    (assembled / "something_else").mkdir()
    (assembled / "stray_file.txt").write_text("irrelevant")
    return assembled


@pytest.fixture
def threat_file(tmp_path: Path) -> str:
    path = tmp_path / "threat.md"
    path.write_text("## Attack surface\npublic internet", encoding="utf-8")
    return str(path.resolve())


@pytest.fixture
def mock_popen_success():
    with patch.object(subprocess, "Popen") as m:
        proc = MagicMock()
        proc.wait.return_value = 0
        m.return_value = proc
        yield m


# ── prompt.py ──────────────────────────────────────────────────


class TestLoadPrompt:
    def test_placeholder_replaced(self, tmp_path, threat_file):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "verifier_prompt.md").write_text(
            "role: verifier\n{{THREAT_MODEL}}\nrules: ...", encoding="utf-8"
        )

        with patch("vuln_verify.prompt.Path", wraps=Path) as mock_path_cls:
            mock_path_cls.side_effect = lambda *a, **kw: Path(*a, **kw)
            result = load_prompt(threat_file)

        assert "{{THREAT_MODEL}}" not in result
        assert "## Attack surface\npublic internet" in result

    def test_threat_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_prompt("/nonexistent/threat.md")


# ── launcher.py ────────────────────────────────────────────────


class TestLaunch:

    def test_spawns_one_process_per_group(self, mock_assembled_dir, threat_file, mock_popen_success):
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file)

        calls = mock_popen_success.call_args_list
        assert len(calls) == 2
        cwds = [kw["cwd"] for _, kw in calls]
        assert sorted(Path(c).name for c in cwds) == ["group_001", "group_002"]

    def test_pi_command_has_required_flags(self, mock_assembled_dir, threat_file, mock_popen_success):
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file)

        cmd = mock_popen_success.call_args_list[0][0][0]
        assert cmd[0] == "pi"
        assert "--session-dir" in cmd
        assert cmd[cmd.index("--session-dir") + 1] == str((mock_assembled_dir.parent / "run").resolve())
        assert "--append-system-prompt" in cmd
        assert "-p" in cmd
        assert any("verifier_output" in a for a in cmd)

    def test_model_flag_passed_to_pi(self, mock_assembled_dir, threat_file, mock_popen_success):
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file, model="test-model:xhigh")

        cmd = mock_popen_success.call_args_list[0][0][0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "test-model:xhigh"

    def test_custom_session_dir_passed_to_pi(self, mock_assembled_dir, threat_file, mock_popen_success):
        session_dir = mock_assembled_dir / "custom-run"
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file, session_dir=session_dir)

        cmd = mock_popen_success.call_args_list[0][0][0]
        idx = cmd.index("--session-dir")
        assert cmd[idx + 1] == str(session_dir.resolve())
        assert session_dir.is_dir()

    def test_no_model_flag_when_omitted(self, mock_assembled_dir, threat_file, mock_popen_success):
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file)

        cmd = mock_popen_success.call_args_list[0][0][0]
        assert "--model" not in cmd

    def test_prompt_includes_secflow_skills_lifecycle(self, mock_assembled_dir, threat_file, mock_popen_success):
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file)

        cmd = mock_popen_success.call_args_list[0][0][0]
        prompt = cmd[cmd.index("-p") + 1]
        assert "SecFlow Skills Lifecycle" in prompt
        assert "wiki-mount" in prompt
        assert "task-collect" in prompt

    def test_concurrency_passed_to_executor(self, mock_assembled_dir, threat_file):
        """Verify ThreadPoolExecutor receives the concurrency value."""
        with (
            patch("vuln_verify.launcher.load_prompt", return_value="prompt"),
            patch("vuln_verify.launcher.ThreadPoolExecutor") as mock_executor,
            patch("vuln_verify.launcher.as_completed", return_value=[]),
            patch("vuln_verify.launcher._verify_one"),
        ):
            launch(mock_assembled_dir, threat_file, concurrency=3)

        mock_executor.assert_called_once_with(max_workers=3)

    def test_verifier_output_dir_created(self, mock_assembled_dir, threat_file, mock_popen_success):
        out_dir = mock_assembled_dir / "verifier_output"
        assert not out_dir.exists()

        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file)

        assert out_dir.is_dir()

    def test_resume_skips_completed_groups(self, mock_assembled_dir, threat_file, mock_popen_success):
        """Groups with .done marker are skipped when resume=True."""
        out_dir = mock_assembled_dir / "verifier_output"
        out_dir.mkdir()
        # Mark group_001 as already done
        (out_dir / "group_001.done").touch()

        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file, resume=True)

        # Only group_002 should be processed
        calls = mock_popen_success.call_args_list
        assert len(calls) == 1
        cwds = [kw["cwd"] for _, kw in calls]
        assert sorted(Path(c).name for c in cwds) == ["group_002"]

    def test_success_writes_done_marker(self, mock_assembled_dir, threat_file, mock_popen_success):
        """Successful groups get a .done marker file."""
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            launch(mock_assembled_dir, threat_file)

        out_dir = mock_assembled_dir / "verifier_output"
        assert (out_dir / "group_001.done").exists()
        assert (out_dir / "group_002.done").exists()

    def test_raises_on_verifier_failure(self, mock_assembled_dir, threat_file):
        with patch.object(subprocess, "Popen") as m, patch(
            "vuln_verify.launcher.load_prompt", return_value="prompt"
        ):
            ok = MagicMock(); ok.wait.return_value = 0
            fail = MagicMock(); fail.wait.return_value = 1
            m.side_effect = [ok, fail]

            with pytest.raises(RuntimeError, match="verifier failed"):
                launch(mock_assembled_dir, threat_file)

    def test_rate_limited_failure_retries_until_success(self, mock_assembled_dir, threat_file):
        with patch("vuln_verify.launcher.load_prompt", return_value="prompt"):
            with patch("vuln_verify.launcher.time.sleep") as mock_sleep:
                call_index = {"count": 0}

                def fake_popen(*args, **kwargs):
                    proc = MagicMock()
                    if call_index["count"] == 0:
                        kwargs["stderr"].write("429 too many requests\n")
                        proc.wait.return_value = 1
                    else:
                        proc.wait.return_value = 0
                    call_index["count"] += 1
                    return proc

                with patch.object(subprocess, "Popen", side_effect=fake_popen) as mock_popen:
                    launch(mock_assembled_dir, threat_file)

        assert mock_popen.call_count == 3
        mock_sleep.assert_called_once_with(30)

    def test_cleans_up_temp_files(self, mock_assembled_dir, threat_file, mock_popen_success):
        orig_unlink = Path.unlink
        removed: list[str] = []

        def tracking_unlink(self):
            removed.append(str(self))
            orig_unlink(self)

        with (
            patch("vuln_verify.launcher.load_prompt", return_value="prompt"),
            patch.object(Path, "unlink", tracking_unlink),
        ):
            launch(mock_assembled_dir, threat_file)

        assert len(removed) == 2
        for p in removed:
            assert "verifier_prompt.md" in p

    def test_no_groups_directory_raises(self, tmp_path, threat_file):
        with pytest.raises(FileNotFoundError, match="groups directory not found"):
            launch(tmp_path / "empty", threat_file)
