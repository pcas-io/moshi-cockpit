"""The presence rule each agent gets (AGENTS.md, Codex developer_instructions). Neutral: no person, no
customer, no data rule in the code. Lines about moshi or ocellio appear only if that
server is configured. The person's own rules (data classes, customers) come from rules.md, which install
never overwrites, and are appended to every agent's rules."""

from __future__ import annotations

from typing import Optional

from cockpit.config import Config
from cockpit.person import IRREVERSIBLE


def presence_text(moshi_name: str, config: Config, role: Optional[str] = None, skill: str = "") -> str:
    lines = [f"# Arbeitsregeln (Agent `{moshi_name}`)", ""]
    if role:
        lines.append(f"Rolle: {role}")
    if config.has("moshi"):
        lines += [
            f"moshi: Deine Identität ist `{moshi_name}`. Zu Sitzungsbeginn `mesh_register` mit Rolle und konkretem "
            "nächsten Ergebnis (`working_on`), danach `mesh_inbox`; nur bei `inbox_pending` > 0 `mesh_receive`.",
            "Vor größeren Aufgaben `working_on` aktualisieren, bei Abschluss oder Blockade `idle`. "
            "Bei Send/Reply immer `context` mitgeben.",
        ]
    if config.has("ocellio"):
        lines.append("ocellio: Zu Sitzungsbeginn `context_load`. Lesen und Berichte (`save_entity`) ohne Nachfrage; "
                     "alles, was Bestehendes ändert, verknüpft, archiviert oder Admin-Funktionen nutzt, fragt nach.")
        if skill:
            lines.append(f"Skill: Lade zu Sitzungsbeginn den ocellio-Skill `{skill}` mit `load_skill` und arbeite danach. "
                         "Lässt er sich nicht laden, sag das in deiner ersten Antwort und arbeite ohne ihn weiter.")
    lines += [
        "Vor jedem irreversiblen Tool (" + ", ".join(f"`{t}`" for t in IRREVERSIBLE) + ") schreibst du zuerst "
        "eine Zeile `IRREVERSIBEL: <tool> → <Ziel>` und wartest auf die Freigabe.",
        "Freigaben nie umgehen: keine Auto-Approve- oder Skip-Optionen, keine Freigabe in fremdem Namen, keine "
        "fremden Identitäten. Lehnt die Person ab oder fehlt eine Oberfläche, gilt das als Nein.",
    ]
    if config.rules:
        # The person's own rules (rules.md): they come last and bind like the rest.
        lines += ["", "## Eigene Regeln (verbindlich)", "", config.rules]
    return "\n".join(lines) + "\n"
