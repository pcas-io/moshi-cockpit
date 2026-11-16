"""Command line entry point. Output and errors never contain secrets."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from cockpit import agents as agent_files
from cockpit import __version__, connect, doctor, installer, launch, registry, removal, sessions, status, wizard
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.config import SERVERS, load_config, require, save_rules
from cockpit.harnesses import Ctx
from cockpit.mcp import McpError
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import Paths
from cockpit.registry import load_agents
from cockpit.secrets import KeychainError, default_store
from cockpit.text import clean

# The panel's "+ Agent" passes --connect and its "Remove" --keep-moshi; both are accepted and change nothing.
PANEL_FLAG = "accepted for the Herdr panel; changes nothing"
NOT_CONNECTED = "{name} is not connected to the servers: this version cannot connect agents yet"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moshi-cockpit", description="Herdr cockpit for agents, optionally with moshi and ocellio")
    parser.add_argument("--version", action="version", version=f"moshi-cockpit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inst = sub.add_parser("install", help="set up the cockpit in the Herdr you installed: safe defaults, mods, agents")
    inst.add_argument("--moshi", metavar="URL", help="moshi server (https://…/mcp); empty (\"\") removes it. Optional")
    inst.add_argument("--ocellio", metavar="URL", help="ocellio server (https://…/mcp/); empty (\"\") removes it. Optional")

    sub.add_parser("uninstall", help="restore every change from the backups")

    run = sub.add_parser("run", help="start an agent (harness, approvals, tokens; used by the menu and the sidebar)")
    run.add_argument("agent")
    run.add_argument("rest", nargs=argparse.REMAINDER)

    doc = sub.add_parser("doctor", help="check the setup (exit code ≠ 0 on errors)")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--quiet", action="store_true", help="silent unless there is an error: then one sentence with one action")

    stat = sub.add_parser("status", help="moshi mesh overview")
    stat.add_argument("--as", dest="identity", help="agent whose moshi token reads the mesh (default: the first one here)")
    stat.add_argument("--json", action="store_true", help="compact JSON (for the sidebar in Herdr)")

    sub.add_parser("connect", help="connect this Herdr to moshi with one approval in the browser")

    agt = sub.add_parser("agent", help="manage agents (name, harness, role)")
    agt_sub = agt.add_subparsers(dest="agent_command", required=True)
    agt_add = agt_sub.add_parser("add", help="create an agent; without arguments as a guided assistant")
    agt_add.add_argument("name", nargs="?")
    agt_add.add_argument("--harness", help="claude, codex, vibe or agy")
    agt_add.add_argument("--role", help="role and personality in a few words")
    agt_add.add_argument("--skill", default="", help="name of an ocellio skill the agent loads at its start")
    agt_add.add_argument("--connect", action="store_true", help=PANEL_FLAG)
    agt_set = agt_sub.add_parser("set", help="change an agent's role or skill")
    agt_set.add_argument("name")
    agt_set.add_argument("--role", help="new role; empty deletes it")
    agt_set.add_argument("--skill", help="new skill; empty deletes it")
    agt_list = agt_sub.add_parser("list", help="show the agents")
    agt_list.add_argument("--json", action="store_true")
    agt_rm = agt_sub.add_parser("remove", help="remove an agent from this Mac: Keychain entries and list entry")
    agt_rm.add_argument("name")
    agt_rm.add_argument("--keep-moshi", action="store_true", help=PANEL_FLAG)

    rul = sub.add_parser("rules", help="your own rules for every agent (e.g. data classes); install never overwrites them")
    rul_sub = rul.add_subparsers(dest="rules_command", required=True)
    rul_sub.add_parser("show", help="show your own rules")
    rul_set = rul_sub.add_parser("set", help="set your own rules (text from stdin or --text) and set the agents up again")
    rul_set.add_argument("--text", help="the rules (without: from stdin); empty removes them")

    ses = sub.add_parser("sessions", help="an agent's sessions that can be resumed (for the session picker)")
    ses.add_argument("agent", nargs="?", help="name of an agent; without it --harness")
    ses.add_argument("--harness", choices=sessions.AGENTS, help="sessions of a CLI instead of an agent (standard agents in the menu)")
    ses.add_argument("--folder", help="only sessions from this folder (default: all)")
    ses.add_argument("--limit", type=int, default=8)
    ses.add_argument("--json", action="store_true")
    return parser


def _context(paths: Paths) -> AgentContext:
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=default_store())


def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    launch.run_agent(paths, default_store(), args.agent, args.rest)
    return 0  # not reached: exec


def cmd_install(args: argparse.Namespace, paths: Paths) -> int:
    ctx = _context(paths)
    notes = installer.run(ctx, installer.InstallOptions(moshi=args.moshi, ocellio=args.ocellio))
    for note in notes:
        print(f"• {note}")
    if installer.HERDR_NOT_READY in notes:
        return 0
    problem = doctor.quiet(doctor.run(doctor.DoctorContext(paths=paths, keychain=ctx.keychain)))
    print(f"\n{problem}" if problem else "\nDone. Continue in Herdr.")
    return 0


def cmd_uninstall(args: argparse.Namespace, paths: Paths) -> int:
    notes = BackupStore(paths.state).restore_all()
    for note in notes or ["nothing to restore"]:
        print(f"• {note}")
    print("Note: Keychain entries and Herdr itself stay.")
    return 0


def cmd_doctor(args: argparse.Namespace, paths: Paths) -> int:
    results = doctor.run(doctor.DoctorContext(paths=paths, keychain=default_store()))
    if args.quiet:
        message = doctor.quiet(results)
        if message:
            print(message)
    else:
        print(doctor.render(results, as_json=args.json))
    return doctor.exit_code(results)


def cmd_status(args: argparse.Namespace, paths: Paths) -> int:
    """Without moshi only the local agents (--json for the panel); with moshi the whole mesh."""
    keychain = default_store()
    config = load_config(paths)
    local = status.local_agents(paths, config, keychain)
    result, identity = {}, ""
    unconnected = connect.needs_connect(paths, config, keychain)
    if config.has("moshi") and not (args.json and unconnected):  # the panel then offers to connect, it gets no error
        try:
            identity = status.pick_identity(keychain, args.identity, [a["name"] for a in local])
        except KeychainError:
            if not args.json:
                raise
            # Connected, but no agent here holds a moshi token yet: the panel gets the local view, not an error.
        if identity:
            result = status.fetch_status(keychain, identity, config.server("moshi"), paths)
    if args.json:
        print(status.as_json(result, identity, local, {name: config.has(name) for name in SERVERS},
                             wizard.harness_offer(paths), status.problems(paths), unconnected))
    elif config.has("moshi"):
        print(status.render(list(result.get("agents") or []), identity))
    else:
        for a in local:
            print(f"{a['name']:<20}{a['harness']:<10}{a['role']}")
        print(f"{len(local)} agent(s) · moshi not set up")
    if not args.json:
        for problem in status.problems(paths):
            print(f"Note: {problem}")
    return 0


def cmd_connect(args: argparse.Namespace, paths: Paths) -> int:
    """Runs in a tab of its own, which closes with the process: the outcome stays readable until Enter
    (not after stdin closed: nobody can press it)."""
    code, wait = 1, True
    try:
        ctx = _context(paths)
        code = connect.run(ctx, connect.ConnectIO(), MoshiApi(require(load_config(paths), "moshi")))
    except (KeychainError, MoshiError, ValueError, OSError) as exc:  # OSError: network, browser
        print(f"Error: {clean(str(exc), '?', limit=500)}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        code = 130
    except EOFError:
        print("\nCancelled.", file=sys.stderr)
        wait = False
    if wait and sys.stdin.isatty():
        try:
            input("\nEnter closes this tab. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


def cmd_agent(args: argparse.Namespace, paths: Paths) -> int:
    ctx = _context(paths)
    if args.agent_command == "set":
        changed = registry.update(paths, ctx.backup, args.name, args.role, args.skill)
        print(f"• {changed.name} updated")
        for note in wizard.skill_note(ctx, changed.skill):
            print(f"! {note}")
        try:  # vibe and agy keep these rules in a file
            agent_files.setup(ctx, changed)
        except ValueError as exc:
            print(f"! Takes effect once the agent is connected: {clean(str(exc), '?', limit=300)}")
        return 0
    if args.agent_command == "list":
        found = load_agents(paths, enforce=False)
        if args.json:
            config = load_config(paths)
            print(json.dumps({"agents": status.local_agents(paths, config, ctx.keychain),
                              "harnesses": wizard.harness_offer(paths),
                              "servers": {name: config.has(name) for name in SERVERS}}, ensure_ascii=False))
        else:
            for a in found:
                print(f"{a.name:<20}{a.harness:<10}{a.role}")
            print(f"{len(found)} agent(s)")
            for problem in registry.single_instance_problems(found):
                print(f"Note: {problem}")
        return 0
    if args.agent_command == "remove":
        for note in removal.remove_agent(ctx, args.name):
            print(f"• {note}")
        return 0
    if args.name and args.harness:
        for note in wizard.create(ctx, args.name, args.harness, args.role or "", args.skill):
            print(f"• {note}")
        if wizard.wants_servers(ctx, args.harness):
            print(f"! {NOT_CONNECTED.format(name=registry.find(paths, args.name).name)}")
        else:
            print(f"\nStart it from the \"+\" menu or with moshi-cockpit run {args.name}")
        return 0
    if not sys.stdin.isatty():
        raise ValueError("Without a terminal, agent add needs a name and --harness")
    for note in wizard.run(ctx, name=args.name, harness=args.harness, role=args.role, skill=args.skill):
        print(f"• {note}")
    return 0


def cmd_rules(args: argparse.Namespace, paths: Paths) -> int:
    if args.rules_command == "show":
        text = load_config(paths).rules
        print(text or "(no rules of your own)")
        return 0
    ctx = _context(paths)
    text = args.text if args.text is not None else sys.stdin.read()
    save_rules(paths, ctx.backup, text)
    print(f"• Your own rules saved ({paths.rules_file}, 0600)")
    for note in installer.setup_agents(ctx):
        print(f"• {note}")
    print("Codex gets them at its next start; Vibe and agy have them in their AGENTS.md now.")
    return 0


def cmd_sessions(args: argparse.Namespace, paths: Paths) -> int:
    if bool(args.agent) == bool(args.harness):
        raise ValueError("Give either an agent or --harness")
    keychain = default_store()
    found = sessions.list_sessions(args.harness, paths, args.folder, args.limit) if args.harness else \
        sessions.for_agent(paths, args.agent, args.folder, args.limit,
                           lambda: Ctx(paths=paths, config=load_config(paths), keychain=keychain))
    if args.json:
        print(sessions.as_json(args.agent or args.harness, args.folder, found))
        return 0
    for s in found:
        print(f"{s['updated'][:16].replace('T', ' ')}  {s['id']}  {s['title']}  ({s['cwd']})")
    print(f"{len(found)} session(s)" + (f" in {args.folder}" if args.folder else ""))
    return 0


COMMANDS = {"install": cmd_install, "uninstall": cmd_uninstall, "doctor": cmd_doctor, "status": cmd_status,
            "run": cmd_run, "sessions": cmd_sessions, "agent": cmd_agent, "rules": cmd_rules, "connect": cmd_connect}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, Paths.from_env())
    except (KeychainError, McpError, ValueError) as exc:
        print(f"Error: {clean(str(exc), '?', limit=500)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
