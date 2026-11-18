"""Inventory: the final harness catalog and the probe that reads a machine's state (no real machine is touched)."""

import os
import re
import subprocess
import tempfile
import unittest

from cockpit import inventory
from tests.helpers import temp_paths
from cockpit.harnesses import HARNESSES
from cockpit.inventory import (BINARIES, INSTALLED, KNOWN, LABELS, LOGIN, MARK, MISSING, PATH_EXTRA, READY,
                               SIGNIN, STATE_TEXT, Probe, held_argv, parse_probe, probe_script)


def line(*fields):
    return "\t".join((MARK,) + fields)


class CatalogTest(unittest.TestCase):
    def test_catalog_is_the_final_six(self):
        self.assertEqual(KNOWN, ("claude", "codex", "vibe", "agy", "grok", "opencode"))
        self.assertEqual(set(BINARIES), set(KNOWN))
        self.assertEqual(set(LABELS), set(KNOWN))
        self.assertEqual(LABELS, {"claude": "Claude Code", "codex": "Codex", "vibe": "Vibe", "agy": "Antigravity",
                                  "grok": "Grok", "opencode": "OpenCode"})

    def test_binaries_match_the_harness_adapters(self):
        self.assertEqual(set(HARNESSES), set(KNOWN) - {"opencode"})
        for name, harness in HARNESSES.items():
            self.assertEqual(BINARIES[name], harness.binary, name)
        self.assertEqual(BINARIES["opencode"], "opencode")

    def test_installed_here_looks_in_the_same_dirs_as_the_probe(self):
        paths = temp_paths(self)
        self.assertEqual(inventory.extra_dirs(paths), [paths.local_bin, paths.home / "bin",
                                                       paths.home / ".opencode" / "bin", paths.home / ".grok" / "bin"])
        nowhere = lambda binary: None
        for name in KNOWN:
            self.assertFalse(inventory.installed_here(name, paths, which=nowhere), name)
        for directory, name in zip(inventory.extra_dirs(paths), ("claude", "vibe", "opencode", "grok")):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text("#!/bin/sh\n")
            self.assertTrue(inventory.installed_here(name, paths, which=nowhere), name)
        self.assertTrue(inventory.installed_here("codex", paths, which=lambda binary: "/usr/bin/" + binary))


class ProbeParseTest(unittest.TestCase):
    def test_probe_states_map_to_the_three_ui_states(self):
        out = "\n".join([line("claude", "1", "1"), line("codex", "1", "0"), line("vibe", "1", "?"),
                         line("agy", "0", "0"), line("grok", "0", "?")]) + "\n"
        probe = parse_probe(out)
        self.assertIsInstance(probe, Probe)
        self.assertEqual(probe.states, {"claude": READY, "codex": INSTALLED, "vibe": INSTALLED, "agy": MISSING,
                                        "grok": MISSING, "opencode": MISSING})
        self.assertEqual(STATE_TEXT, {READY: "Ready", INSTALLED: "Installed, not signed in", MISSING: "Missing"})
        self.assertEqual((READY, INSTALLED, MISSING), ("ready", "installed", "missing"))

    def test_noise_around_the_probe_lines_is_ignored(self):
        out = "\r\n".join([
            "Welcome to devbox! Last login: Tue from build-1",
            "claude\t1\t1",                       # no marker: profile noise, never a state
            "need\tcurl\t1",
            line("codex", "1", "1"),
            "  " + line("vibe", "1", "1"),          # marker not at the start: noise
            line("grok", "1", "1", "extra"),        # wrong field count
            line("agy", "yes", "1"),                # not a 0/1 field
            line("nope", "1", "1"),                 # not in the catalog
            "* System restart required *",
        ]) + "\r\n"
        probe = parse_probe(out)
        self.assertEqual(probe.states, {"claude": MISSING, "codex": READY, "vibe": MISSING, "agy": MISSING,
                                        "grok": MISSING, "opencode": MISSING})
        self.assertFalse(probe.has_curl)

    def test_curl_presence_is_reported(self):
        self.assertTrue(parse_probe(line("need", "curl", "1") + "\n").has_curl)
        self.assertFalse(parse_probe(line("need", "curl", "0") + "\n").has_curl)
        self.assertFalse(parse_probe("").has_curl)
        self.assertEqual(set(parse_probe("").states.values()), {MISSING})


class ProbeScriptTest(unittest.TestCase):
    def test_every_harness_has_a_pinned_check_and_login(self):
        self.assertEqual(set(SIGNIN), set(KNOWN))
        self.assertEqual(LOGIN, {"claude": ("claude", "auth", "login"), "codex": ("codex", "login", "--device-auth"),
                                 "vibe": ("vibe", "--setup"), "agy": ("agy",),
                                 "grok": ("grok", "login", "--device-auth"),
                                 "opencode": ("opencode", "auth", "login")})
        for name, argv in LOGIN.items():
            self.assertEqual(argv[0], BINARIES[name], name)
        self.assertNotIn("security", SIGNIN["claude"])

    def test_probe_script_never_prints_vendor_output(self):
        script = probe_script()
        for name, check in SIGNIN.items():
            if check:
                self.assertIn("{ " + check + "; } >/dev/null 2>&1 </dev/null", script, name)
        for name in KNOWN:
            self.assertIn(f"command -v {BINARIES[name]} >/dev/null 2>&1", script, name)

    def test_probe_script_has_no_sudo_and_reads_no_file_content(self):
        script = probe_script()
        for word in ("sudo", "cat", "head", "tail", "less", "more", "source"):
            self.assertIsNone(re.search(rf"\b{word}\b", script), word)
        self.assertIsNone(re.search(r"(^|\s)-[wg](\s|$)", script))
        self.assertNotIn(" . ", script)
        # stdin is redirected only from /dev/null, never from a file
        self.assertEqual(set(re.findall(r"<\s*([^\s;]+)", script)), {"/dev/null"})

    def test_probe_script_is_static(self):
        self.assertEqual(probe_script(), probe_script())
        self.assertIn(f'PATH="$PATH:{PATH_EXTRA}"', probe_script())
        for part in ("$HOME/.local/bin", "$HOME/bin", "$HOME/.opencode/bin", "$HOME/.grok/bin"):
            self.assertIn(part, PATH_EXTRA.split(":"))

    def test_probe_script_parses_under_sh(self):
        done = subprocess.run(["/bin/sh", "-n", "-c", probe_script()], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_probe_script_runs_under_sh(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as empty_bin:
            env = {"HOME": home, "PATH": f"{empty_bin}:/usr/bin:/bin"}
            done = subprocess.run(["/bin/sh", "-c", probe_script()], env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        probe = parse_probe(done.stdout)
        self.assertEqual(probe.states, {name: MISSING for name in KNOWN})
        self.assertEqual(probe.has_curl, os.path.exists("/usr/bin/curl") or os.path.exists("/bin/curl"))

    def test_signed_in_and_unknown_checks_reach_their_states(self):
        """Fake binaries in $HOME/.local/bin: found through PATH_EXTRA, the check decides ready or installed."""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as empty_bin:
            local = os.path.join(home, ".local", "bin")
            os.makedirs(local)
            for name in ("codex", "agy"):
                path = os.path.join(local, name)
                with open(path, "w") as f:  # `codex login status` exits 0: signed in
                    f.write("#!/bin/sh\necho 'must not be seen'\nexit 0\n")
                os.chmod(path, 0o755)
            env = {"HOME": home, "PATH": f"{empty_bin}:/usr/bin:/bin"}
            done = subprocess.run(["/bin/sh", "-c", probe_script()], env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotIn("must not be seen", done.stdout + done.stderr)
        states = parse_probe(done.stdout).states
        self.assertEqual((states["codex"], states["agy"]), (READY, INSTALLED))  # agy: no documented check

    def run_with_fake_claude(self, code, credentials):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as empty_bin:
            local = os.path.join(home, ".local", "bin")
            os.makedirs(local)
            with open(os.path.join(local, "claude"), "w") as f:
                f.write(f"#!/bin/sh\nexit {code}\n")
            os.chmod(os.path.join(local, "claude"), 0o755)
            if credentials:
                os.makedirs(os.path.join(home, ".claude"))
                open(os.path.join(home, ".claude", ".credentials.json"), "w").close()
            env = {"HOME": home, "PATH": f"{empty_bin}:/usr/bin:/bin"}
            done = subprocess.run(["/bin/sh", "-c", probe_script()], env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return parse_probe(done.stdout).states["claude"]

    def test_claude_status_decides_and_the_file_only_counts_when_it_cannot_answer(self):
        self.assertEqual(self.run_with_fake_claude(0, credentials=False), READY)
        self.assertEqual(self.run_with_fake_claude(1, credentials=True), INSTALLED)   # "not logged in" wins
        self.assertEqual(self.run_with_fake_claude(2, credentials=True), READY)       # cannot answer: the file
        self.assertEqual(self.run_with_fake_claude(2, credentials=False), INSTALLED)

    def test_held_argv_waits_for_enter_and_keeps_the_command(self):
        argv = held_argv("codex login")
        self.assertEqual(argv[:2], ["/bin/sh", "-lc"])
        self.assertEqual(argv[2], f'PATH="$PATH:{PATH_EXTRA}"; codex login; code=$?; '
                                  'printf "\\nFinished (exit %s). Press Enter to close.\\n" "$code"; read -r _')
        done = subprocess.run(["/bin/sh", "-c", argv[2].replace("codex login", "false")], input="\n",
                              capture_output=True, text=True, timeout=30)
        self.assertIn("Finished (exit 1). Press Enter to close.", done.stdout)


if __name__ == "__main__":
    unittest.main()
