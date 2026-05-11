"""
pandabot_core.config
~~~~~~~~~~~~~~~~~~~~
Environment variable loading, feature flags, and config helpers.

Usage in a bot:
    from pandabot_core.config import cfg
    cfg.require("DISCORD_TOKEN", "ANTHROPIC_API_KEY")
    cfg.load()   # call once at startup; raises ConfigError on missing required vars

    token = cfg.str("DISCORD_TOKEN")
    enabled = cfg.flag("ENABLE_JELLYFIN", default=False)
    services = cfg.csv_set("SYSTEMD_SERVICES", "sunshine,tailscaled")
    logs = cfg.csv_dict("FILE_LOGS", "")
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger("pandabot.config")

__all__ = ["Config", "cfg", "ConfigError"]


class ConfigError(RuntimeError):
    pass


class Config:
    """Lazy env-var accessor with validation."""

    def __init__(self) -> None:
        self._required: list[str] = []

    def require(self, *var_names: str) -> "Config":
        """Declare one or more env vars as required. Call before load()."""
        self._required.extend(var_names)
        return self

    def load(self) -> None:
        """Validate all required vars are present. Raises ConfigError listing missing ones."""
        missing = [v for v in self._required if not os.environ.get(v)]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}\n"
                "Set them in your .env file or systemd unit."
            )
        log.info("Config validated — %d required var(s) present", len(self._required))

    # ------------------------------------------------------------------
    # Accessors — never raise; return the default when a var is unset
    # ------------------------------------------------------------------

    def str(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    def int(self, name: str, default: int = 0) -> int:
        raw = os.environ.get(name, "")
        try:
            return int(raw) if raw else default
        except ValueError:
            log.warning("Config: %s=%r is not an integer, using default %d", name, raw, default)
            return default

    def flag(self, name: str, default: bool = False) -> bool:
        """Boolean feature flag. Reads 'true'/'false' (case-insensitive)."""
        raw = os.environ.get(name, "")
        if not raw:
            return default
        return raw.strip().lower() == "true"

    def csv_set(self, name: str, default: str = "") -> set[str]:
        """Parse a comma-separated env var into a set of stripped strings."""
        raw = os.environ.get(name, default)
        return {s.strip() for s in raw.split(",") if s.strip()}

    def csv_list(self, name: str, default: str = "") -> list[str]:
        """Parse a comma-separated env var into a list of stripped strings."""
        raw = os.environ.get(name, default)
        return [s.strip() for s in raw.split(",") if s.strip()]

    def csv_dict(self, name: str, default: str = "") -> dict[str, str]:
        """Parse 'key:value,key:value' env var into a dict."""
        raw = os.environ.get(name, default)
        result: dict[str, str] = {}
        for item in raw.split(","):
            item = item.strip()
            if ":" in item:
                k, _, v = item.partition(":")
                result[k.strip()] = v.strip()
        return result

    def data_dir(self) -> str:
        """
        Writable directory for SQLite databases and other persistent state.
        Defaults to PANDABOT_DATA_DIR env var, or the current working directory.
        """
        path = os.environ.get("PANDABOT_DATA_DIR", "")
        if path:
            os.makedirs(path, exist_ok=True)
            return path
        return os.getcwd()

    def db_path(self, filename: str = "pandabot.db") -> str:
        """Absolute path to a database file inside data_dir."""
        return os.path.join(self.data_dir(), filename)


# Module-level singleton — bots import and use this directly
cfg = Config()
