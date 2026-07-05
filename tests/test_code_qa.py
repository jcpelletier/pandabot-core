"""Tests for pandabot_core.code_qa — codebase Q&A."""
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from pandabot_core.code_qa import _get_repo_path, _clone_or_update, list_files, search_pattern, read_file, query_codebase

@pytest.fixture
def mock_repo(tmp_path):
    """Create a fake repo structure in the temp data dir."""
    repo_dir = tmp_path / "repo_cache" / "pandabot-core"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    # Add some files
    (repo_dir / "README.md").write_text("Hello World")
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "main.py").write_text("print('hello')\n# line 2")

    return repo_dir

def test_get_repo_path():
    path = _get_repo_path("pandabot-core")
    assert "repo_cache/pandabot-core" in path

    with pytest.raises(ValueError):
        _get_repo_path("invalid-repo")

def test_clone_or_update_clones(tmp_path):
    with patch("subprocess.run") as mock_run:
        repo_name = "pandabot-core"
        # Dir doesn't exist yet
        _clone_or_update(repo_name)

        assert mock_run.called
        args = mock_run.call_args[0][0]
        assert "clone" in args
        assert "https://github.com/jcpelletier/pandabot-core.git" in args

def test_clone_or_update_pulls(mock_repo):
    with patch("subprocess.run") as mock_run:
        _clone_or_update("pandabot-core")

        assert mock_run.called
        args = mock_run.call_args[0][0]
        assert "pull" in args

def test_list_files(mock_repo):
    files = list_files("pandabot-core")
    assert "README.md" in files
    assert "src/main.py" in files
    assert ".git" not in files

def test_search_pattern(mock_repo):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="./src/main.py:1:print('hello')\n")
        res = search_pattern("pandabot-core", "print")
        assert "main.py" in res
        assert "print('hello')" in res

def test_read_file(mock_repo):
    content = read_file("pandabot-core", "src/main.py")
    assert "print('hello')" in content

    # Too large
    content = read_file("pandabot-core", "src/main.py", max_size_kb=0)
    assert "Error: File too large" in content

    # Path traversal
    content = read_file("pandabot-core", "../../etc/passwd")
    assert "Error: Path traversal" in content

@patch("pandabot_core.code_qa._clone_or_update")
@patch("pandabot_core.llm.provider.get_provider")
def test_query_codebase_flow(mock_get_provider, mock_update, mock_repo):
    mock_provider = MagicMock()
    mock_get_provider.return_value = mock_provider
    mock_provider.primary_model = "deepseek-chat"

    # Step 1: identify files
    mock_provider.complete_simple.side_effect = [
        ("pandabot-core:src/main.py", 10, 10),  # relevant files
        ("The main file prints hello. (pandabot-core:src/main.py:1)", 20, 20)  # answer
    ]

    ans = query_codebase("What does the main file do?", repo_names=["pandabot-core"])

    assert "main file prints hello" in ans
    assert "(pandabot-core:src/main.py:1)" in ans
    assert mock_update.called
    mock_get_provider.assert_called_with("deepseek")
