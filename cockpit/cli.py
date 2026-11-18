"""Command line entry point. Output and errors never contain secrets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from cockpit import agents as agent_files
from cockpit import __version__, agents_remote, connect, doctor, harness_install, hostkeys, hosts, installer, launch, legacy, machines, registry, removal, sessions, status, wizard
from cockpit.agents import AgentContext
from cockpit.agents_remote import ApprovalRequired
from cockpit.backup import BackupStore
from cockpit.config import SERVERS, load_config, require, save_rules
from cockpit.harnesses import Ctx
from cockpit.mcp import McpError
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import Paths
from cockpit.registry import load_agents
from cockpit.secrets import KeychainError, default_store
from cockpit.text import clean

APPROVAL_EXIT = 3  # shared with the mods: the panel opens the connect dialog
ACTIVE_ELSEWHERE_EXIT = 4  # shared with the mods: stdout {"active": label}, the panel offers a takeover
TAKE_OVER = "--take-over"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moshi-cockpit", description="Herdr cockpit for agents, optionally with moshi and ocellio")
    parser.add_argument("--version", action="version", version=f"moshi-cockpit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inst = sub.add_parser("install", help="set up the cockpit in the Herdr you installed: safe defaults, mods, agents")
    inst.add_argument("--moshi", metavar="URL", help="moshi server (https://…/mcp); empty (\"\") removes it. Optional")
    inst.add_argument("--ocellio", metavar="URL", help="ocellio server (https://…/mcp/); empty (\"\") removes it. Optional")

    sub.add_parser("uninstall", help="restore every change from the backups")

    run = sub.add_parser("run", help="start an agent (harness, approvals, tokens; used by the menu and the sidebar)")
    run.add_argument(TAKE_OVER, action="store_true",
                     help="start here even when another installation holds the agent (also right after the name)")
    run.add_argument("agent")
    run.add_argument("rest", nargs=argparse.REMAINDER)

    doc = sub.add_parser("doctor", help="check the setup (exit code ≠ 0 on errors)")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--quiet", action="store_true", help="silent unless there is an error: then one sentence with one action")

    stat = sub.add_parser("status", help="moshi mesh overview (read with this Herdr's device key)")
    stat.add_argument("--json", action="store_true", help="compact JSON (for the sidebar in Herdr)")

    sub.add_parser("connect", help="connect this Herdr to moshi with one approval in the browser")
    sub.add_parser("migrate", help="move the agents of an earlier setup to this installation (after connect)")

    agt = sub.add_parser("agent", help="manage agents (name, harness, role)")
    agt_sub = agt.add_subparsers(dest="agent_command", required=True)
    agt_add = agt_sub.add_parser("add", help="create an agent (in moshi too, when set up); without arguments guided")
    agt_add.add_argument("name", nargs="?")
    agt_add.add_argument("--harness", help="claude, codex, vibe or agy")
    agt_add.add_argument("--role", help="role and personality in a few words")
    agt_add.add_argument("--skill", default="", help="name of an ocellio skill the agent loads at its start")
    agt_load = agt_sub.add_parser("load", help="add an agent that exists in moshi to this Mac")
    agt_load.add_argument("name")
    agt_cat = agt_sub.add_parser("catalog", help="moshi's agents that are not on this Mac yet")
    agt_cat.add_argument("--json", action="store_true")
    agt_where = agt_sub.add_parser("where", help="the installation an agent is active on")
    agt_where.add_argument("name")
    agt_where.add_argument("--json", action="store_true")
    agt_set = agt_sub.add_parser("set", help="change an agent's role or skill (here and in moshi)")
    agt_set.add_argument("name")
    agt_set.add_argument("--role", help="new role; empty deletes it")
    agt_set.add_argument("--skill", help="new skill; empty deletes it")
    agt_list = agt_sub.add_parser("list", help="show the agents")
    agt_list.add_argument("--json", action="store_true")
    agt_rm = agt_sub.add_parser("remove", help="remove an agent from this Mac; it stays in moshi")
    agt_rm.add_argument("name")

    rul = sub.add_parser("rules", help="your own rules for every agent (e.g. data classes); install never overwrites them")
    rul_sub = rul.add_subparsers(dest="rules_command", required=True)
    rul_sub.add_parser("show", help="show your own rules")
    rul_set = rul_sub.add_parser("set", help="set your own rules (text from stdin or --text) and set the agents up again")
    rul_set.add_argument("--text", help="the rules (without: from stdin); empty removes them")

    har = sub.add_parser("harness", help="install a missing agent CLI with its vendor's official installer")
    har_sub = har.add_subparsers(dest="harness_command", required=True)
    har_inst = har_sub.add_parser("install", help="ask, then run the vendor's installer")
    har_inst.add_argument("name", choices=sorted(harness_install.INSTALL))

    mac = sub.add_parser("machine", help="the machines Herdr reaches over ssh and the agent CLIs on each")
    mac_sub = mac.add_subparsers(dest="machine_command", required=True)
    mac_list = mac_sub.add_parser("list", help="Herdr's machines and the ssh and saved hosts not added yet")
    mac_list.add_argument("--json", action="store_true")
    mac_inv = mac_sub.add_parser("inventory", help="which agent CLIs a machine has and whether each is signed in")
    mac_inv.add_argument("--on", metavar="NAME", help="a machine from `machine list` (default: this Mac)")
    mac_inv.add_argument("--refresh", action="store_true", help="check again, even within a minute of the last check")
    mac_inv.add_argument("--json", action="store_true")
    mac_add = mac_sub.add_parser("add", help="add an ssh host to Herdr (hc machine add), then show its agent CLIs")
    mac_add.add_argument("alias", help="a Host from ~/.ssh/config, user@host or user@host:port")
    mac_add.add_argument("--batch", action="store_true",
                         help="without a terminal (the Machines tab): installs herdr-core there, never asks; "
                              "a password or an unknown host key comes back as the answer")
    mac_add.add_argument("--json", action="store_true")
    mac_hosts = mac_sub.add_parser("hosts", help="your own list of hosts that are not in ~/.ssh/config")
    mac_hosts.add_argument("hosts_command", choices=["add", "remove"])
    mac_hosts.add_argument("target")
    mac_key = mac_sub.add_parser("hostkey", help="whether a host's key is known, else the keys it offers")
    mac_key.add_argument("target")
    mac_key.add_argument("--json", action="store_true")
    mac_trust = mac_sub.add_parser("trust", help="add a host's key to ~/.ssh/known_hosts after checking it again")
    mac_trust.add_argument("target")
    mac_trust.add_argument("--type", required=True, help="the key type `machine hostkey` showed")
    mac_trust.add_argument("--fingerprint", required=True, help="the SHA256:… fingerprint `machine hostkey` showed")
    mac_trust.add_argument("--json", action="store_true")

    ses = sub.add_parser("sessions", help="an agent's sessions that can be resumed (for the session picker)")
    ses.add_argument("agent", nargs="?", help="name of an agent; without it --harness")
    ses.add_argument("--harness", choices=sessions.AGENTS, help="sessions of a CLI instead of an agent (standard agents in the menu)")
    ses.add_argument("--folder", help="only sessions from this folder (default: all)")
    ses.add_argument("--limit", type=int, default=8)
    ses.add_argument("--json", action="store_true")
    return parser


def _context(paths: Paths) -> AgentContext:
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=default_store())


def _on_terminal() -> bool:
    """A person is looking: a Herdr tab or a terminal, both ends a TTY."""
    return sys.stdin.isatty() and sys.stdout.isatty()


CLOSE_PROMPT = "Press Enter to close this tab."


def _hold_tab() -> None:
    """A Herdr tab closes with its process: what happened stays readable until Enter."""
    try:
        input(f"\n{CLOSE_PROMPT} ")
    except (EOFError, KeyboardInterrupt):
        pass


def _ask_take_over(label: str) -> bool:
    try:
        answer = input(f"Running on {clean(label, '?', limit=64)}. Take over here? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    """`run <agent> [--take-over] [args…]`: the flag right after the name is ours, everything later the harness's.
    The start is prepared (command, tokens, binary) before moshi is asked, so a start that cannot happen takes
    nothing over. On a terminal it asks before a takeover, and a failure waits for Enter."""
    rest, take_over = list(args.rest), args.take_over
    if rest[:1] == [TAKE_OVER]:
        rest, take_over = rest[1:], True
    terminal = _on_terminal()
    keychain = default_store()
    try:
        start = launch.prepare(paths, keychain, args.agent, rest)
        launch.claim(paths, keychain, args.agent, take_over, confirm=_ask_take_over if terminal else None)
        launch.execute(start)
    except launch.ActiveElsewhere as exc:
        if terminal:
            return ACTIVE_ELSEWHERE_EXIT  # the person said no
        print(json.dumps({"active": exc.label}, ensure_ascii=False))  # stdout stays JSON for the panel
        print(f"{args.agent} is active on {exc.label}. To start it here: moshi-cockpit run {args.agent} {TAKE_OVER}",
              file=sys.stderr)
        return ACTIVE_ELSEWHERE_EXIT
    except FAILURES as exc:
        if not terminal:
            raise
        code = report(exc)
        _hold_tab()
        return code
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
    """Undoes every change outside the person's own settings folder; that folder and the Keychain stay."""
    settings = paths.mesh_config.parent
    notes = BackupStore(paths.state).restore_all(keep=settings)
    for note in notes or ["nothing to restore"]:
        print(f"• {note}")
    app = paths.home / ".local" / "share" / "moshi-cockpit"

    def short(path: Path) -> str:
        return "~/" + str(path.relative_to(paths.home)) if paths.home in path.parents else str(path)

    print(f"Kept: your settings in {short(settings)}/ (config.json, agents.json, rules.md), the Keychain entries "
          f"(moshi-… and ocellio-…), the app in {short(app)}/, the state folder {short(paths.state)}/ and Herdr itself. "
          f"Earlier copies of your settings (*.bak-…) stay in {short(settings)}/ too.")
    print(f"To remove them by hand: rm -rf {short(settings)} {short(app)} {short(paths.state)}, delete the moshi-… "
          "and ocellio-… entries in Keychain Access, and revoke this installation in the moshi dashboard.")
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
    mesh = status.read_mesh(paths, config, keychain)
    unconnected = connect.needs_connect(paths, config, keychain)
    if args.json:
        print(status.as_json(mesh, local, {name: config.has(name) for name in SERVERS}, wizard.harness_offer(paths),
                             status.problems(paths), unconnected, connect.approval_valid(paths, keychain)))
        return 0
    if config.has("moshi") and not unconnected:
        print(status.render(mesh))
    else:
        for a in local:
            print(f"{a['name']:<20}{a['harness']:<10}{a['role']}")
        print(f"{len(local)} agent(s) · " + ("not connected to moshi: run moshi-cockpit connect" if unconnected
                                             else "moshi not set up"))
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
        _hold_tab()
    return code


def cmd_migrate(args: argparse.Namespace, paths: Paths) -> int:
    ctx = _context(paths)
    for note in legacy.migrate(ctx, agents_remote.api_for(require(load_config(paths), "moshi"))):
        print(note if note == legacy.REVOKE_HINT else f"• {note}")
    return 0


def cmd_agent(args: argparse.Namespace, paths: Paths) -> int:
    ctx = _context(paths)
    handler = {"set": _agent_set, "list": _agent_list, "remove": _agent_remove, "load": _agent_load,
               "catalog": _agent_catalog, "where": _agent_where}.get(args.agent_command, _agent_add)
    return handler(ctx, args)


def _agent_add(ctx: AgentContext, args: argparse.Namespace) -> int:
    if args.name and args.harness:
        notes = wizard.create(ctx, args.name, args.harness, args.role or "", args.skill) + [wizard.start_hint(args.name)]
    elif not sys.stdin.isatty():
        raise ValueError("Without a terminal, agent add needs a name and --harness")
    else:
        notes = wizard.run(ctx, name=args.name, harness=args.harness, role=args.role, skill=args.skill)
    for note in notes:
        print(f"• {note}")
    return 0


def _agent_load(ctx: AgentContext, args: argparse.Namespace) -> int:
    agent = agents_remote.load(ctx, args.name)
    print(f"• Agent {agent.name} ({agent.harness}) loaded from moshi")
    for note in agents_remote.notes(ctx, agent) + wizard.skill_note(ctx, agent.skill):
        print(f"• {note}")
    print(f"• {wizard.start_hint(agent.name)}")
    return 0


def _agent_catalog(ctx: AgentContext, args: argparse.Namespace) -> int:
    rows = agents_remote.catalog(ctx)
    if args.json:
        print(json.dumps({"agents": rows}, ensure_ascii=False))
        return 0
    for row in rows:
        where = f"running on {row['active_installation']}" if row["active_installation"] else ""
        print(f"{row['name']:<20}{row['harness']:<10}{row['role'][:30]:<32}{where}")
    print(f"{len(rows)} agent(s) in moshi that are not here yet")
    return 0


def _agent_where(ctx: AgentContext, args: argparse.Namespace) -> int:
    holder = status.where(ctx.paths, load_config(ctx.paths), ctx.keychain, registry.validate_name(args.name))
    if args.json:
        print(json.dumps({"active": holder["active"], "here": holder["here"]}, ensure_ascii=False))
    elif holder["here"]:
        print(f"{args.name} is active on this Herdr ({holder['active']})")
    else:
        print(f"{args.name} is active on {holder['active']}" if holder["active"] else f"{args.name} is active nowhere")
    return 0


def _agent_set(ctx: AgentContext, args: argparse.Namespace) -> int:
    changed, warnings = agents_remote.set_profile(ctx, args.name, args.role, args.skill)
    print(f"• {changed.name} updated")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    for note in wizard.skill_note(ctx, changed.skill):
        print(f"! {note}")
    try:  # vibe and agy keep these rules in a file
        agent_files.setup(ctx, changed)
    except ValueError as exc:
        print(f"! Takes effect once the agent is connected: {clean(str(exc), '?', limit=300)}")
    return 0


def _agent_list(ctx: AgentContext, args: argparse.Namespace) -> int:
    paths = ctx.paths
    found = load_agents(paths, enforce=False)
    if args.json:
        config = load_config(paths)
        print(json.dumps({"agents": status.local_agents(paths, config, ctx.keychain),
                          "harnesses": wizard.harness_offer(paths),
                          "servers": {name: config.has(name) for name in SERVERS}}, ensure_ascii=False))
        return 0
    for a in found:
        print(f"{a.name:<20}{a.harness:<10}{a.role}")
    print(f"{len(found)} agent(s)")
    for problem in registry.single_instance_problems(found):
        print(f"Note: {problem}")
    return 0


def _agent_remove(ctx: AgentContext, args: argparse.Namespace) -> int:
    notes, warnings = removal.remove_agent(ctx, args.name)
    for note in notes:
        print(f"• {note}")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
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


def cmd_harness(args: argparse.Namespace, paths: Paths) -> int:
    """Runs in a tab of its own: on a terminal the outcome waits for Enter, success or failure."""
    interactive = _on_terminal()
    try:
        code = harness_install.run_cli(args.name, paths, harness_install.InstallIO(interactive=interactive))
    except FAILURES as exc:
        if not interactive:
            raise
        code = report(exc)
    if interactive:
        _hold_tab()
    return code


def cmd_machine(args: argparse.Namespace, paths: Paths) -> int:
    if args.machine_command == "list":
        found = machines.list_machines(paths, machines.run_capture)
        if args.json:
            print(json.dumps(found, ensure_ascii=False))
            return 0
        for m in found["machines"]:
            print(f"{clean(m['name'], '?', limit=40):<24}{clean(m['state'], '?', limit=20):<14}{clean(m['target'], '', limit=80)}")
        if found["ssh_hosts"]:
            print("Not added yet (from ~/.ssh/config): " + ", ".join(clean(h, "?", limit=80) for h in found["ssh_hosts"]))
        if found["saved_hosts"]:
            print("Not added yet (saved): " + ", ".join(clean(h, "?", limit=80) for h in found["saved_hosts"]))
        if found.get("problem"):
            print(f"Note: {found['problem']}")
        return 0
    if args.machine_command == "inventory":
        result = machines.inventory(paths, machines.check_target(args.on) if args.on else None, machines.run_capture,
                                    refresh=args.refresh)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
            return 0
        print(f"{args.on or machines.THIS_MAC}, checked {result['checked_at']}" + (" (cached)" if result["cached"] else ""))
        for row in result["harnesses"]:
            print(f"{row['label']:<14}{row['state_text']}")
        return 0
    if args.machine_command == "add" and not args.batch:
        return _machine_add(args, paths)
    return _machine_flow(args, paths)


def _machine_flow(args: argparse.Namespace, paths: Paths) -> int:
    """The Machines tab's add flow, one JSON answer per step (text without --json): hosts, hostkey, trust,
    add --batch. A structured answer of `add --batch` (ok or not) exits 0; only our own errors exit 1."""
    store = BackupStore(paths.state)
    if args.machine_command == "hosts":
        change = hosts.add_host if args.hosts_command == "add" else hosts.remove_host
        change(paths, store, args.target)
        print(json.dumps({"ok": True}))
        return 0
    if args.machine_command == "hostkey":
        result = hostkeys.hostkey(args.target, machines.run_capture, paths.home)
        text = result.get("message") or ("known" if result["known"] else
                                         "  ".join(f"{k['type']} {k['sha256']}" for k in result["keys"]))
    elif args.machine_command == "trust":
        _, host, port = hostkeys.resolve(args.target, machines.run_capture, paths.home)
        hostkeys.trust(host, port, args.type, args.fingerprint, machines.run_capture, paths.home, args.target)
        result, text = {"ok": True}, f"{args.target}: host key trusted"
    else:
        result = machines.add_batch(paths, store, args.alias, machines.run_capture, machines.run_batch)
        text = result.get("message") or (f"{result['machine']} was added" if result["ok"] else "not added")
    print(json.dumps(result, ensure_ascii=False) if args.json else clean(text, "?", limit=500))
    return 0


def _machine_add(args: argparse.Namespace, paths: Paths) -> int:
    """Runs in a tab of its own: hc's prompts appear in it, and on a terminal the outcome waits for Enter."""
    interactive = _on_terminal()
    try:
        code = machines.add_machine(paths, args.alias, harness_install.run_argv, machines.run_capture)
        code = 1 if code else 0  # hc's own message is on screen; our exit codes stay 0/1/3/4
    except FAILURES as exc:
        if not interactive:
            raise
        code = report(exc)
    if interactive:
        _hold_tab()
    return code


COMMANDS = {"install": cmd_install, "uninstall": cmd_uninstall, "doctor": cmd_doctor, "status": cmd_status,
            "run": cmd_run, "sessions": cmd_sessions, "agent": cmd_agent, "rules": cmd_rules, "connect": cmd_connect,
            "harness": cmd_harness, "migrate": cmd_migrate, "machine": cmd_machine}


# What a command ends with on a known failure: one line on stderr and an exit code, never a traceback.
FAILURES = (ApprovalRequired, MoshiError, KeychainError, McpError, ValueError, OSError)


def report(exc: BaseException) -> int:
    """Prints the one line for a failure and returns its exit code (3 for a missing approval)."""
    if isinstance(exc, ApprovalRequired):
        print(clean(str(exc), "?", limit=500), file=sys.stderr)
        return APPROVAL_EXIT
    if isinstance(exc, MoshiError):
        exc = agents_remote.refusal(exc)
    elif isinstance(exc, OSError) and exc.strerror:  # "[Errno 13] …" reads badly; the path stays
        exc = ValueError(f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror)
    print(f"Error: {clean(str(exc), '?', limit=500)}", file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, Paths.from_env())
    except FAILURES as exc:
        return report(exc)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
