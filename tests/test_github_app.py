"""pandabot_core.github_app: per-bot GitHub App installation tokens."""
import os

import pytest

from pandabot_core import github_app as ga

APP_VARS = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in APP_VARS + ("GH_TOKEN",):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "static")
    ga._reset_for_tests()
    yield
    ga._reset_for_tests()


def _configure(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/nowhere.pem")


def test_unconfigured_returns_empty_and_leaves_env_alone():
    assert not ga.configured()
    assert ga.get_token() == ""
    assert os.environ["GITHUB_TOKEN"] == "static"


def test_partial_config_is_not_configured(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    assert not ga.configured() and ga.get_token() == ""


def test_mints_caches_and_exports(monkeypatch):
    _configure(monkeypatch)
    now = [1000.0]
    minted = []
    monkeypatch.setattr(ga.time, "time", lambda: now[0])
    monkeypatch.setattr(ga, "_mint", lambda: minted.append(1) or (f"ghs_{len(minted)}", now[0] + 3600))
    assert ga.get_token() == "ghs_1"
    assert os.environ["GITHUB_TOKEN"] == os.environ["GH_TOKEN"] == "ghs_1"
    now[0] += 3000                       # still more than 5 minutes left: cached
    assert ga.get_token() == "ghs_1" and len(minted) == 1
    now[0] += 400                        # inside the 5-minute window: refreshed
    assert ga.get_token() == "ghs_2" and len(minted) == 2


def test_failed_refresh_keeps_a_still_valid_token(monkeypatch):
    _configure(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(ga.time, "time", lambda: now[0])
    monkeypatch.setattr(ga, "_mint", lambda: ("ghs_a", now[0] + 3600))
    ga.get_token()

    def boom():
        raise RuntimeError("github down")
    monkeypatch.setattr(ga, "_mint", boom)
    now[0] += 3400                       # refresh due, token still valid for 200 s
    assert ga.get_token() == "ghs_a"
    now[0] += 300                        # now expired
    assert ga.get_token() == ""


def test_first_mint_failure_returns_empty(monkeypatch):
    _configure(monkeypatch)

    def boom():
        raise RuntimeError("bad key")
    monkeypatch.setattr(ga, "_mint", boom)
    assert ga.get_token() == ""
