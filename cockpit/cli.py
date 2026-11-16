"""Command line entry point. Output and errors are German, never contain secrets."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from cockpit import agents as agent_files
from cockpit import __version__, admin, agenttest, bots, connect, doctor, installer, launch, person, register, removal, sessions, status, wizard
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.keychain import Keychain, KeychainError
from cockpit.mcp import McpError
from cockpit.config import SERVERS, load_config, origin, require, save_rules
from cockpit.tokens import ADMIN, REGISTER, bind_identity
from cockpit.harnesses import Ctx
from cockpit.paths import Paths
from cockpit import registry
from cockpit.registry import load_agents
from cockpit.pins import PinError, load_pins
from cockpit.text import clean


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moshi-cockpit", description="herdr-Cockpit für Agents, optional mit moshi und ocellio")
    parser.add_argument("--version", action="version", version=f"moshi-cockpit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inst = sub.add_parser("install", help="herdr prüfen/installieren, sichere Defaults, Agents einrichten")
    inst.add_argument("--skip-herdr", action="store_true", help="herdr weder prüfen noch installieren")
    inst.add_argument("--person", help="für wen die Agents arbeiten; moshi-Namen werden <person>-<agent>")
    inst.add_argument("--moshi", metavar="URL", help="moshi-Server (https://…/mcp); leer (\"\") entfernt ihn. Optional")
    inst.add_argument("--ocellio", metavar="URL", help="ocellio-Server (https://…/mcp/); leer (\"\") entfernt ihn. Optional")

    sub.add_parser("uninstall", help="alle Änderungen aus den Backups wiederherstellen")

    run = sub.add_parser("run", help="einen Agent starten (Harness, Freigaben, Tokens; von Menü und Sidebar genutzt)")
    run.add_argument("agent")
    run.add_argument("rest", nargs=argparse.REMAINDER)

    doc = sub.add_parser("doctor", help="Zustand prüfen (Exit ≠ 0 bei Fehlern)")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--quiet", action="store_true", help="still, außer bei einem Fehler: dann ein Satz mit genau einer Handlung")

    stat = sub.add_parser("status", help="moshi-Mesh-Übersicht")
    stat.add_argument("--as", dest="identity", help="moshi-Identität zum Lesen (Standard: cockpit, sonst erster Agent)")
    stat.add_argument("--json", action="store_true", help="kompakte JSON-Ausgabe (für die Sidebar in herdr)")

    sub.add_parser("connect", help="diesen Mac mit moshi und ocellio verbinden (Tokens werden verdeckt abgefragt)")

    bot = sub.add_parser("bot", help="moshi-Identitäten verwalten")
    bot_sub = bot.add_subparsers(dest="bot_command", required=True)
    add = bot_sub.add_parser("add", help="Identität anbinden (Connect-Seite + verdeckte Token-Eingabe)")
    add.add_argument("agent", help="Name eines Agents (moshi-cockpit doctor zeigt sie) oder cockpit")
    add.add_argument("--strict-keychain", action="store_true", help="macOS fragt bei jedem Lesen nach (-T \"\")")
    add.add_argument("--browser", action="store_true",
                     help="Connect-Seite statt moshi-API, auch wenn ein Registrier- oder Admin-Token da ist")
    add.add_argument("--rebind", action="store_true", help="Tokens absichtlich an eine geänderte Server-URL binden")
    add.add_argument("--auto", action="store_true", help="ohne Rückfragen: bricht ab, wenn etwas fehlt, statt nachzufragen")
    adm = bot_sub.add_parser("admin-token", help="moshi-Admin-Token in die Keychain (immer mit Bestätigungsdialog)")
    adm.add_argument("--rebind", action="store_true", help="absichtlich an eine geänderte moshi-URL binden")
    reg = bot_sub.add_parser("register-token", help="Registrier-Token: darf nur Agents anlegen (statt Admin-Token)")
    reg_sub = reg.add_subparsers(dest="register_command", required=True)
    reg_new = reg_sub.add_parser("new", help="ausstellen (Admin-Rechner); landet nur in der Zwischenablage")
    reg_new.add_argument("--person", required=True, help="für wen; erlaubt nur moshi-Namen <person>-…")
    reg_new.add_argument("--days", type=int, default=register.DEFAULT_DAYS,
                         help=f"gültig für N Tage (Standard {register.DEFAULT_DAYS}, höchstens {register.MAX_DAYS})")
    reg_new.add_argument("--max-per-hour", type=int, help="höchstens N neue Agents pro Stunde (Standard von moshi: 10)")
    reg_new.add_argument("--label", help="Bezeichnung in der Liste")
    reg_set = reg_sub.add_parser("set", help="hinterlegen (Agent-Rechner); immer mit Bestätigungsdialog")
    reg_set.add_argument("--rebind", action="store_true", help="absichtlich an eine geänderte moshi-URL binden")
    reg_set.add_argument("--clipboard", action="store_true",
                         help="aus der Zwischenablage lesen statt einfügen; danach wird sie geleert")
    reg_sub.add_parser("remove", help="hinterlegtes Registrier-Token hier entfernen (z. B. nach revoke)")
    reg_sub.add_parser("list", help="ausgestellte Registrier-Tokens anzeigen (Admin-Rechner)")
    reg_rev = reg_sub.add_parser("revoke", help="sofort ungültig machen (Admin-Rechner)")
    reg_rev.add_argument("id")

    agt = sub.add_parser("agent", help="Agents verwalten (Name, Harness, Rolle)")
    agt_sub = agt.add_subparsers(dest="agent_command", required=True)
    agt_add = agt_sub.add_parser("add", help="Agent anlegen; ohne Angaben als geführter Assistent")
    agt_add.add_argument("name", nargs="?")
    agt_add.add_argument("--harness", help="claude, codex, vibe oder agy")
    agt_add.add_argument("--role", help="Rolle und Persönlichkeit in wenigen Worten")
    agt_add.add_argument("--skill", default="", help="Name eines ocellio-Skills, den der Agent beim Start lädt")
    agt_add.add_argument("--connect", action="store_true", help="gleich bei den Servern anmelden, ohne Rückfragen")
    agt_set = agt_sub.add_parser("set", help="Rolle oder Skill eines Agents ändern")
    agt_set.add_argument("name")
    agt_set.add_argument("--role", help="neue Rolle; leer löscht sie")
    agt_set.add_argument("--skill", help="neuer Skill; leer löscht ihn")
    agt_list = agt_sub.add_parser("list", help="Agents anzeigen")
    agt_list.add_argument("--json", action="store_true")
    agt_rm = agt_sub.add_parser("remove", help="Agent entfernen: moshi-Identität widerrufen (mit Admin-Zugang), "
                                "Keychain-Einträge löschen, aus der Liste nehmen")
    agt_rm.add_argument("name")
    agt_rm_mode = agt_rm.add_mutually_exclusive_group()
    agt_rm_mode.add_argument("--revoke-moshi", action="store_true", help="moshi-Identität ohne Rückfrage widerrufen")
    agt_rm_mode.add_argument("--keep-moshi", action="store_true", help="moshi-Identität aktiv lassen")

    rul = sub.add_parser("rules", help="eigene Regeln für alle Agents (z. B. Datenklassen); install überschreibt sie nie")
    rul_sub = rul.add_subparsers(dest="rules_command", required=True)
    rul_sub.add_parser("show", help="eigene Regeln anzeigen")
    rul_set = rul_sub.add_parser("set", help="eigene Regeln setzen (Text von stdin oder --text), Agents neu einrichten")
    rul_set.add_argument("--text", help="Regeltext (ohne: von stdin); leer entfernt die Regeln")

    ses = sub.add_parser("sessions", help="fortsetzbare Sessions eines Agents (für die Session-Auswahl)")
    ses.add_argument("agent", nargs="?", help="Name eines Agents; ohne ihn --harness")
    ses.add_argument("--harness", choices=sessions.AGENTS, help="Sessions einer CLI statt eines Agents (Standard-Agents im Menü)")
    ses.add_argument("--folder", help="nur Sessions aus diesem Ordner (Standard: alle)")
    ses.add_argument("--limit", type=int, default=8)
    ses.add_argument("--json", action="store_true")

    test = sub.add_parser("test", help="Agent-Test ocellio + moshi, danach working_on wiederherstellen")
    test.add_argument("agents", nargs="*", help="Agents (Standard: alle mit Harness vibe, agy oder codex)")
    test.add_argument("--report-to", default="cockpit",
                      help="moshi-Empfänger der Bericht-Links (Standard: cockpit)")
    return parser


def _context(paths: Paths) -> AgentContext:
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=Keychain())


def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    launch.run_agent(paths, Keychain(), args.agent, args.rest)
    return 0  # not reached: exec


def _moshi(paths: Paths) -> str:
    return require(load_config(paths), "moshi")


def _agent_names(paths: Paths) -> List[str]:
    return [a.name for a in load_agents(paths, enforce=False)]


def cmd_install(args: argparse.Namespace, paths: Paths) -> int:
    ctx = _context(paths)
    if args.person:
        changed = person.save_person(paths, ctx.backup, args.person)
        who = person.load_person(paths)
        print(f"• Person: {who} (moshi-Namen <{who}>-<agent>)" + ("" if changed else ", unverändert"))
    options = installer.InstallOptions(skip_herdr=args.skip_herdr, moshi=args.moshi, ocellio=args.ocellio)
    notes = installer.run(ctx, load_pins(), options)
    for note in notes:
        print(f"• {note}")
    if installer.HERDR_NOT_READY in notes:
        return 0
    problem = None
    if not args.skip_herdr:  # with it herdr may be missing on purpose
        problem = doctor.quiet(doctor.run(doctor.DoctorContext(paths=paths, keychain=ctx.keychain, pins=load_pins())))
    print(f"\n{problem}" if problem else "\nFertig. Weiter geht es in Herdr.")
    return 0


def cmd_uninstall(args: argparse.Namespace, paths: Paths) -> int:
    notes = BackupStore(paths.state).restore_all()
    for note in notes or ["nichts wiederherzustellen"]:
        print(f"• {note}")
    print("Hinweis: Keychain-Einträge und herdr selbst bleiben bestehen.")
    return 0


def cmd_doctor(args: argparse.Namespace, paths: Paths) -> int:
    ctx = doctor.DoctorContext(paths=paths, keychain=Keychain(), pins=load_pins())
    results = doctor.run(ctx)
    if args.quiet:
        message = doctor.quiet(results)
        if message:
            print(message)
    else:
        print(doctor.render(results, as_json=args.json))
    return doctor.exit_code(results)


def cmd_status(args: argparse.Namespace, paths: Paths) -> int:
    """Without moshi only the local agents (--json for the panel); with moshi the whole mesh."""
    keychain = Keychain()
    config = load_config(paths)
    local = status.local_agents(paths, config, keychain)
    result, identity = {}, ""
    unconnected = connect.needs_connect(paths, config, keychain)
    if config.has("moshi") and not (args.json and unconnected):  # the panel then offers to connect, it gets no error
        identity = status.pick_identity(keychain, args.identity, [a["name"] for a in local])
        result = status.fetch_status(keychain, identity, config.server("moshi"), paths)
    if args.json:
        print(status.as_json(result, identity, local, {name: config.has(name) for name in SERVERS},
                             wizard.harness_offer(paths), status.problems(paths), admin.has_admin_token(keychain),
                             unconnected))
    elif config.has("moshi"):
        print(status.render(list(result.get("agents") or []), identity))
    else:
        for a in local:
            print(f"{a['name']:<20}{a['harness']:<10}{a['role']}")
        print(f"{len(local)} Agent(s) · moshi nicht eingerichtet")
    if not args.json:
        for problem in status.problems(paths):
            print(f"Hinweis: {problem}")
    return 0


def cmd_connect(args: argparse.Namespace, paths: Paths) -> int:
    """Runs in a tab of its own, which closes with the process: the outcome stays readable until Enter."""
    code = 0
    ctx = _context(paths)
    try:
        for note in connect.run(ctx):
            print(f"• {note}")
        print(f"\n{connect.DONE}")
    except (KeychainError, McpError, admin.AdminError, ValueError, OSError) as exc:  # OSError: network
        print(f"Fehler: {clean(str(exc), '?', limit=500)}", file=sys.stderr)
        if connect.worth_retrying(ctx, exc):
            print(connect.KEPT, file=sys.stderr)
        code = 1
    if sys.stdin.isatty():
        try:
            input("\nEnter schließt diesen Tab. ")
        except EOFError:
            pass
    return code


def cmd_bot(args: argparse.Namespace, paths: Paths) -> int:
    if args.bot_command == "admin-token":
        url = _moshi(paths)
        print(f"Das Admin-Token wird an {origin(url)} gebunden und nur dorthin gesendet.")
        token = bots.ask_token(bots.BotIO(), "moshi-Admin-Token einfügen (Eingabe unsichtbar): ")
        bound = bind_identity(paths, BackupStore(paths.state), ADMIN, url, rebind=args.rebind)
        admin.store_admin_token(Keychain(), token)
        print(f"• {admin.ADMIN_SERVICE}: gespeichert und an {bound} gebunden, macOS fragt bei jedem Lesen nach")
        return 0
    if args.bot_command == "register-token":
        return cmd_register_token(args, paths)
    for note in bots.add(args.agent, _context(paths), strict=args.strict_keychain, browser=args.browser,
                         rebind=args.rebind, auto=args.auto):
        print(f"• {note}")
    return 0


def cmd_register_token(args: argparse.Namespace, paths: Paths) -> int:
    if args.register_command == "remove":  # works without a configured moshi, too
        for note in register.forget_register_token(Keychain(), paths, BackupStore(paths.state)):
            print(f"• {note}")
        return 0
    url, keychain = _moshi(paths), Keychain()
    if args.register_command == "set":
        print(f"Das Registrier-Token wird an {origin(url)} gebunden und nur dorthin gesendet.")
        if args.clipboard:
            token = register.read_clipboard()
        else:
            token = bots.ask_token(bots.BotIO(), "Registrier-Token (rt_…) einfügen (Eingabe unsichtbar): ")
        register.validate_register_token(token)
        try:
            bound = bind_identity(paths, BackupStore(paths.state), REGISTER, url, rebind=args.rebind)
            register.store_register_token(keychain, token)
        except (ValueError, KeychainError) as exc:
            if args.clipboard:  # keep it for the second try, but say so
                raise ValueError(f"{exc}. Das Registrier-Token liegt noch in der Zwischenablage") from None
            raise
        print(f"• {register.REGISTER_SERVICE}: gespeichert und an {bound} gebunden, macOS fragt bei jedem Lesen nach")
        if args.clipboard:
            print("• Zwischenablage geleert" if register.clear_clipboard(token) else
                  "• Zwischenablage NICHT geleert (inzwischen etwas anderes darin oder pbcopy fehlgeschlagen): "
                  "bitte selbst prüfen und leeren")
        if not load_config(paths).person:
            print("• Hinweis: Hier ist keine Person eingerichtet. moshi-Namen sind dann die Agent-Namen ohne Präfix, "
                  "das Registrier-Token nimmt aber nur <person>-…. Setzen mit: moshi-cockpit install --person <name>")
        print("Neue Agents: moshi-cockpit agent add")
        return 0
    if not admin.has_admin_token(keychain):
        raise ValueError("Ausstellen, Anzeigen und Widerrufen brauchen das Admin-Token: moshi-cockpit bot admin-token")
    if args.register_command == "new" and not register.clipboard_ready():
        raise ValueError("Keine Zwischenablage (pbcopy) gefunden; das Registrier-Token wird nie angezeigt")
    api = admin.AdminApi.from_keychain(keychain, url, paths)
    if args.register_command == "list":
        print(register.render_tokens(api.registration_tokens()))
    elif args.register_command == "revoke":
        gone = api.revoke_registration_token(clean(args.id, "", limit=40).upper())
        print(f"• {clean(gone.get('id'), '?')} widerrufen ({clean(gone.get('revoked_at'), '?')[:16].replace('T', ' ')})")
    else:
        created = register.issue(api, args.person, args.days, args.max_per_hour, args.label)
        print(f"• Registrier-Token {clean(created.get('id'), '?')} für {clean(created.get('name_prefix'), '?')}… "
              f"bis {clean(created.get('expires_at'), '?')[:16].replace('T', ' ')} UTC")
        print("• Es liegt in der Zwischenablage und wird nicht angezeigt. Sicher übergeben (nicht per Mail/Chat im "
              "Klartext), danach die Zwischenablage leeren.")
        print("Auf dem Rechner der Person: moshi-cockpit bot register-token set --clipboard")
    return 0


def cmd_agent(args: argparse.Namespace, paths: Paths) -> int:
    ctx = _context(paths)
    if args.agent_command == "set":
        changed = registry.update(paths, ctx.backup, args.name, args.role, args.skill)
        print(f"• {changed.name} aktualisiert")
        for note in wizard.skill_note(ctx, changed.skill):
            print(f"! {note}")
        try:  # vibe and agy keep these rules in a file
            agent_files.setup(ctx, changed)
        except ValueError as exc:
            print(f"! Wirkt nach dem Anmelden: {clean(str(exc), '?', limit=300)}")
        return 0
    if args.agent_command == "list":
        found = load_agents(paths, enforce=False)
        if args.json:
            config = load_config(paths)
            print(json.dumps({"agents": status.local_agents(paths, config, ctx.keychain),
                              "harnesses": wizard.harness_offer(paths), "admin": admin.has_admin_token(ctx.keychain),
                              "servers": {name: config.has(name) for name in SERVERS}}, ensure_ascii=False))
        else:
            for a in found:
                print(f"{a.name:<20}{a.harness:<10}{a.role}")
            print(f"{len(found)} Agent(s)")
            for problem in registry.single_instance_problems(found):
                print(f"Hinweis: {problem}")
        return 0
    if args.agent_command == "remove":
        revoke = True if args.revoke_moshi else False if args.keep_moshi else None
        tty = sys.stdin.isatty()  # without a terminal nobody can answer: no revoke unless --revoke-moshi
        confirm = (lambda q: input(q).strip().lower() in ("", "j", "ja", "y", "yes")) if tty else (lambda _q: False)
        for note in removal.remove_agent(ctx, args.name, revoke=revoke, confirm=confirm):
            print(f"• {note}")
        return 0
    if args.name and args.harness:
        for note in wizard.create(ctx, args.name, args.harness, args.role or "", args.skill):
            print(f"• {note}")
        if args.connect and wizard.wants_servers(ctx, args.harness):
            try:  # the agent exists either way; what is missing is said, not a stack of steps
                for note in bots.add(registry.find(paths, args.name).name, ctx, auto=True):
                    print(f"• {note}")
            except (ValueError, KeychainError, admin.AdminError, McpError, OSError) as exc:  # OSError: network
                print(f"! Noch nicht angemeldet: {clean(str(exc), '?', limit=300)}")
            return 0
        if wizard.wants_servers(ctx, args.harness):
            print(f"\nServer anbinden: moshi-cockpit bot add {args.name}")
        else:
            print("\nStarten: im „+“-Menü oder mit moshi-cockpit run " + args.name)
        return 0
    if not sys.stdin.isatty():
        raise ValueError("Ohne Terminal braucht agent add einen Namen und --harness")
    connect = lambda name: bots.add(name, ctx)  # noqa: E731
    for note in wizard.run(ctx, name=args.name, harness=args.harness, role=args.role, connect=connect, skill=args.skill):
        print(f"• {note}")
    return 0


def cmd_rules(args: argparse.Namespace, paths: Paths) -> int:
    if args.rules_command == "show":
        text = load_config(paths).rules
        print(text or "(keine eigenen Regeln)")
        return 0
    ctx = _context(paths)
    text = args.text if args.text is not None else sys.stdin.read()
    save_rules(paths, ctx.backup, text)
    print(f"• Eigene Regeln gespeichert ({paths.rules_file}, 0600)")
    for note in installer.setup_agents(ctx):
        print(f"• {note}")
    print("Codex bekommt sie beim nächsten Start, Vibe und agy haben sie jetzt in ihrer AGENTS.md.")
    return 0


def cmd_sessions(args: argparse.Namespace, paths: Paths) -> int:
    if bool(args.agent) == bool(args.harness):
        raise ValueError("Entweder einen Agent oder --harness angeben")
    keychain = Keychain()
    found = sessions.list_sessions(args.harness, paths, args.folder, args.limit) if args.harness else \
        sessions.for_agent(paths, args.agent, args.folder, args.limit,
                           lambda: Ctx(paths=paths, config=load_config(paths), keychain=keychain))
    if args.json:
        print(sessions.as_json(args.agent or args.harness, args.folder, found))
        return 0
    for s in found:
        print(f"{s['updated'][:16].replace('T', ' ')}  {s['id']}  {s['title']}  ({s['cwd']})")
    print(f"{len(found)} Session(s)" + (f" in {args.folder}" if args.folder else ""))
    return 0


def cmd_test(args: argparse.Namespace, paths: Paths) -> int:
    chosen = args.agents or [a.name for a in load_agents(paths) if a.harness in ("vibe", "agy", "codex")]
    return agenttest.run(Keychain(), chosen, args.report_to, paths=paths)


COMMANDS = {"install": cmd_install, "uninstall": cmd_uninstall, "doctor": cmd_doctor,
            "status": cmd_status, "bot": cmd_bot, "test": cmd_test, "run": cmd_run,
            "sessions": cmd_sessions, "agent": cmd_agent, "rules": cmd_rules, "connect": cmd_connect}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, Paths.from_env())
    except (KeychainError, McpError, PinError, admin.AdminError, ValueError) as exc:
        print(f"Fehler: {clean(str(exc), '?', limit=500)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nabgebrochen", file=sys.stderr)
        return 130
