"""All harnesses moshi-cockpit can drive. A new agent CLI is one class here, nothing else."""

from cockpit.harnesses.account import Claude
from cockpit.harnesses.agy import Agy
from cockpit.harnesses.base import Ctx, Harness
from cockpit.harnesses.codex import Codex
from cockpit.harnesses.grok import Grok
from cockpit.harnesses.vibe import Vibe

HARNESSES = {h.name: h for h in (Claude(), Codex(), Vibe(), Agy(), Grok())}

__all__ = ["HARNESSES", "Ctx", "Harness"]
