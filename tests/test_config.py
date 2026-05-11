import os
import pytest
from pandabot_core.config import Config, ConfigError


def test_flag_true():
    os.environ["ENABLE_FOO"] = "true"
    cfg = Config()
    assert cfg.flag("ENABLE_FOO") is True
    del os.environ["ENABLE_FOO"]


def test_flag_false_default():
    os.environ.pop("ENABLE_BAR", None)
    cfg = Config()
    assert cfg.flag("ENABLE_BAR", default=False) is False


def test_csv_set():
    os.environ["MY_SET"] = "a, b, c"
    cfg = Config()
    assert cfg.csv_set("MY_SET") == {"a", "b", "c"}
    del os.environ["MY_SET"]


def test_csv_dict():
    os.environ["MY_DICT"] = "foo:/var/log/foo.log,bar:/var/log/bar.log"
    cfg = Config()
    result = cfg.csv_dict("MY_DICT")
    assert result == {"foo": "/var/log/foo.log", "bar": "/var/log/bar.log"}
    del os.environ["MY_DICT"]


def test_require_missing_raises():
    os.environ.pop("MISSING_VAR", None)
    cfg = Config()
    cfg.require("MISSING_VAR")
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        cfg.load()


def test_require_present_passes():
    os.environ["PRESENT_VAR"] = "hello"
    cfg = Config()
    cfg.require("PRESENT_VAR")
    cfg.load()  # should not raise
    del os.environ["PRESENT_VAR"]


def test_db_path_uses_data_dir(tmp_path):
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    cfg = Config()
    assert cfg.db_path("test.db") == str(tmp_path / "test.db")
    del os.environ["PANDABOT_DATA_DIR"]
