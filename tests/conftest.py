"""
Shared pytest fixtures for pandabot-core tests.
Sets PANDABOT_DATA_DIR to a temp directory so tests never touch real state.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path):
    """Point all DB writes to a per-test temp directory."""
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    yield
    os.environ.pop("PANDABOT_DATA_DIR", None)


@pytest.fixture(autouse=True)
def clear_github_pm_env():
    """Ensure GitHub PM env vars don't bleed between tests."""
    for key in ("ENABLE_GITHUB_PM", "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPOS"):
        os.environ.pop(key, None)
    yield
