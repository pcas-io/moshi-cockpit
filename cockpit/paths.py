"""Locations. Everything derives from one home directory so tests can use a temp HOME. No server URLs and
no agent names here: servers come from config.py, agents from registry.py."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

USER_AGENT = "moshi-cockpit/0.1"

# The moshi identity of the human at the cockpit (reads the mesh for the sidebar). Reserved as an agent name.
COCKPIT_IDENTITY = "cockpit"

REPO_ROOT = Path(__file__).resolve().parent.parent
SHARE = REPO_ROOT / "share"
BIN = REPO_ROOT / "bin"


@dataclass(frozen=True)
class Paths:
    """All filesystem locations moshi-cockpit reads or writes."""

    home: Path
    herdr_app_home: Path
    applications: Path

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Paths":
        env = os.environ if env is None else env
        home = Path(env.get("HOME") or str(Path.home()))
        app_home = env.get("HERDR_APP_HOME")
        if not app_home:
            config_root = env.get("XDG_CONFIG_HOME") or str(home / ".config")
            app_home = str(Path(config_root) / "herdr-app")
        return cls(home=home, herdr_app_home=Path(app_home), applications=Path("/Applications"))

    @property
    def config(self) -> Path:
        return self.home / ".config"

    @property
    def local_bin(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def state(self) -> Path:
        return self.home / ".local" / "state" / "moshi-cockpit"

    @property
    def herdr_settings(self) -> Path:
        return self.herdr_app_home / "settings.json"

    @property
    def herdr_socket(self) -> Path:
        return self.herdr_app_home / "herdr-app.sock"

    @property
    def herdr_mods(self) -> Path:
        return self.herdr_app_home / "mods"

    @property
    def herdr_installed(self) -> Path:
        return self.herdr_app_home / "installed.json"

    @property
    def herdr_core_bin(self) -> Path:
        return self.local_bin / "herdr-core"

    @property
    def herdr_app_bundle(self) -> Path:
        return self.applications / "Herdr.app"

    @property
    def agents_file(self) -> Path:
        return self.config / "moshi-cockpit" / "agents.json"

    def agent_home(self, name: str) -> Path:
        """Per-agent files a harness needs (e.g. a Vibe profile)."""
        return self.home / ".local" / "share" / "moshi-cockpit" / "agents" / name

    @property
    def rules_file(self) -> Path:
        """The person's own rules for every agent (never written by install, see config.load_rules)."""
        return self.config / "moshi-cockpit" / "rules.md"

    @property
    def mesh_config(self) -> Path:
        return self.config / "moshi-cockpit" / "config.json"

    @property
    def grok_config(self) -> Path:
        return self.home / ".grok" / "config.toml"

    @property
    def standard_vibe_home(self) -> Path:
        """Where Vibe keeps its data when started without a profile of ours (the standard entry)."""
        return self.home / ".vibe"

    @property
    def legacy_vibe_home(self) -> Path:
        """The Vibe profile of the first installation (agent "vibe"); kept by the migration."""
        return self.home / ".vibe-mesh"

    @property
    def agy_mcp_config(self) -> Path:
        return self.home / ".gemini" / "config" / "mcp_config.json"

    @property
    def agy_settings(self) -> Path:
        return self.home / ".gemini" / "antigravity-cli" / "settings.json"

    @property
    def agy_agents_md(self) -> Path:
        return self.home / ".gemini" / "AGENTS.md"


def keychain_service(server: str, agent: str) -> str:
    """Keychain naming convention: service '<server>-<agent>', account '<agent>'."""
    return f"{server}-{agent}"
