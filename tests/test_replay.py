from pathlib import Path
import shutil
import subprocess

import pytest

from repo_atlas.replay import replay_history


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for history replay")
def test_replay_compares_real_commit_states(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "RepoAtlas test"], check=True)
    source = tmp_path / "service.py"
    source.write_text("def original():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "service.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "first"], check=True, capture_output=True)
    source.write_text("def renamed():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "service.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "second"], check=True, capture_output=True)

    assert replay_history(tmp_path, commits=2)["commits_checked"] == 2