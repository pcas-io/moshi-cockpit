"""tools/leak_check.py: the guard that keeps internal ids, private emails and blocked words out of the repo.

Test inputs that would trip the guard on this very file are assembled at runtime, so the file stays clean.
The blocked words here are made up; the real list lives outside the repo.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Dict, List

TOOL = Path(__file__).resolve().parent.parent / "tools" / "leak_check.py"
_spec = importlib.util.spec_from_file_location("leak_check", TOOL)
leak_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak_check)

WORD = "zebracornia"  # stands in for an entry of the private word list
HOST = "quux" + "corp.example"  # a dotted entry
WORDS = frozenset({WORD, HOST})
SHORT_ID = "06a" + "1b2c3"
UUID7 = "0190a1b2-c3d4-7" + "e5f-8a9b-0c1d2e3f4a5b"
PRIVATE_EMAIL = "dev" + "@" + "corp-mail.de"
HASH = "#"


class FakeGit:
    """Answers the two git questions the guard asks: which files, and what each commit says."""

    def __init__(self, commits: Dict[str, str] = None) -> None:
        self.commits = dict(commits or {})
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str], cwd: Path) -> str:
        self.calls.append(list(args))
        if args[0] == "ls-files":
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
        if args[0] == "rev-list":
            return "".join(f"{sha}\n" for sha in self.commits)
        if args[0] == "show":
            return self.commits[args[-1]]
        raise AssertionError(args)


def rules(line: str) -> List[str]:
    return leak_check.scan_line(line, WORDS)


class LeakCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_guard(self, argv=None, git=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = leak_check.main(argv or [], words=WORDS, cwd=self.root, git=git or FakeGit())
        return code, out.getvalue()

    def test_leak_check_flags_an_ocellio_id(self):
        self.write("a.py", f"x = 1\n# see {SHORT_ID}\n")
        self.write("docs/b.md", f"id {UUID7}\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertIn("a.py:2: ocellio-id", out)
        self.assertIn("docs/b.md:1: ocellio-id", out)
        self.assertNotIn(SHORT_ID, out)

    def test_an_eight_hex_token_is_an_id_only_next_to_an_id_word(self):
        short = "c0ffee42"  # made up
        for line in (f"Refs: {short}", f"Ref: {short}", f"ocellio entity {short}", f"id: {short}", f"Closes {short}"):
            with self.subTest(line=line):
                self.assertEqual(rules(line), ["ocellio-id"])
        for line in (f"fixed in {short} yesterday", "git show " + "5eed1e5" + "a", "id: 12345678", "Refs: deadbeef0",
                     f"identity {short}", f"prefs {short}", f"the entity_{short}"):
            with self.subTest(line=line):
                self.assertEqual(rules(line), [])

    def test_leak_check_flags_a_blocked_word_and_prints_only_the_rule(self):
        self.write("a.txt", "fine\n")
        self.write("b.txt", f"ok\nAgent {WORD.capitalize()}-codex at mcp.{WORD}.example now.\n")
        self.write("c.txt", f"host {WORD}.\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["b.txt:2: blocked-word", "c.txt:1: blocked-word"])
        self.assertNotIn(WORD, out.lower())

    def test_every_run_of_atoms_counts(self):
        for text in (f"https://api.{WORD}/x", f"{WORD}-labs", f"svc.{WORD}.internal", f"x-{WORD}",
                     f"my-{WORD}-agent", f"api-{HOST}", f"https://mcp.{HOST}/v1", f"{HOST}-backup"):
            with self.subTest(text=text):
                self.assertEqual(rules(text), ["blocked-word"])
        self.assertEqual(rules(f"{WORD}x and x{WORD} and {HOST[:4]}-{HOST[4:]}"), [])

    def test_camel_case_compounds_are_split(self):
        camel = WORD.capitalize()
        for text in (f"{camel}Agent", f"new{camel}", f"the{camel}Bot runs", f"{WORD.upper()}Client",
                     f"v2{camel}", f"HTTP{camel}Api"):
            with self.subTest(text=text):
                self.assertEqual(rules(text), ["blocked-word"])

    def test_issue_references_are_flagged(self):
        for line in (f"see moshi{HASH}4242", f"fixed in PR {HASH}9001", f"pr{HASH}777", f"issue {HASH}31337",
                     f"acme/tool{HASH}808", f"Closes {HASH}606", f"(moshi {HASH}4242 not deployed)"):
            with self.subTest(line=line):
                self.assertEqual(rules(line), ["issue-ref"])
        for line in ("color: #000000;", 'fill="#123"', "echo $#", "# 1. step", "x = a[#1]", "#!/bin/sh",
                     "  #4711 in the list", "#1a2b3c"):
            with self.subTest(line=line):
                self.assertEqual(rules(line), [])

    def test_decision_codes_are_flagged(self):
        for line in ("residual risk " + "77", "(Restrisiko " + "88)", "WP-" + "999 says", "see the A" + "DR"):
            with self.subTest(line=line):
                self.assertEqual(rules(line), ["decision-code"])
        for line in ("Q", "QA team", "planned for Q3 2026", "a quick win", "adr", "residual risk is low", "WP-x"):
            with self.subTest(line=line):
                self.assertEqual(rules(line), [])

    def test_leak_check_allows_example_test_and_noreply_emails(self):
        allowed = ["a@example.test", "b@example.invalid", "cosrv@users.noreply.github.com", "noreply@anthropic.com"]
        self.write("ok.md", "\n".join(f"mail {mail}" for mail in allowed) + "\n")
        self.assertEqual(self.run_guard(), (0, ""))
        self.write("bad.md", f"write to {PRIVATE_EMAIL}\n")
        code, out = self.run_guard()
        self.assertEqual((code, out), (1, "bad.md:1: email\n"))

    def test_leak_check_flags_a_blocked_word_in_a_commit_message_and_a_private_author_email(self):
        clean = f"{'0' * 40}\ncosrv\ncosrv@users.noreply.github.com\ncosrv\ncosrv@users.noreply.github.com\nchore: initial import\n"
        dirty = (f"{'0' * 40}\ncosrv\n{PRIVATE_EMAIL}\ncosrv\ncosrv@users.noreply.github.com\n"
                 f"feat: talk to {WORD}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
        git = FakeGit({"a" * 40: clean, "b" * 40: dirty})
        code, out = self.run_guard(["--commits", "origin/main..HEAD"], git=git)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"commit {'b' * 12}:2: email", f"commit {'b' * 12}:5: blocked-word"])
        self.assertIn(["rev-list", "origin/main..HEAD"], git.calls)
        self.assertIn(["show", "-s", "--format=%P%n%an%n%ae%n%cn%n%ce%n%B", "b" * 40], git.calls)

    def test_githubs_own_merge_commits_pass_and_nothing_else_is_let_through(self):
        me, gh = "cosrv\ncosrv@users.noreply.github.com", "GitHub\nnoreply" + "@github.com"
        two = f"{'1' * 40} {'2' * 40}"

        def commit(parents, author, committer, message):
            return f"{parents}\n{author}\n{committer}\n{message}\n"

        merge = commit(two, me, gh, f"Merge pull request {HASH}12 from cosrv/fix-sidebar\n\nfix: the sidebar line")
        history = {"a" * 40: commit("0" * 40, me, me, "chore: initial import"), "b" * 40: merge,
                   "c" * 40: commit(two, gh, gh, f"Merge pull request {HASH}3 from cosrv/docs")}
        code, out = self.run_guard(["--commits", "c410f3e..HEAD"], git=FakeGit(history))
        self.assertEqual((code, out), (0, ""))
        bad = {
            "d" * 40: commit(two, me, gh, f"Merge pull request {HASH}13 from cosrv/{WORD}"),  # a word in the branch
            "e" * 40: commit(two, me, gh, f"Merge pull request {HASH}14 from cosrv/x\n\nsee {SHORT_ID}"),  # the body
            "f" * 40: commit("0" * 40, me, gh, f"Merge pull request {HASH}15 from cosrv/x"),  # not a merge
            "1" * 40: commit(two, me, me, f"Merge pull request {HASH}16 from cosrv/x"),  # not GitHub's committer
            "2" * 40: commit(two, me, gh, f"Merge branch 'main' into x, closes {HASH}17"),  # not GitHub's subject
            "3" * 40: commit("0" * 40, gh, me, "feat: a commit by noreply"),  # GitHub's address elsewhere
        }
        code, out = self.run_guard(["--commits", "c410f3e..HEAD"], git=FakeGit({**history, **bad}))
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            f"commit {'d' * 12}:5: blocked-word", f"commit {'e' * 12}:7: ocellio-id",
            f"commit {'f' * 12}:4: email", f"commit {'f' * 12}:5: issue-ref",
            f"commit {'1' * 12}:5: issue-ref", f"commit {'2' * 12}:4: email", f"commit {'2' * 12}:5: issue-ref",
            f"commit {'3' * 12}:2: email"])

    def test_leak_check_passes_a_clean_tree(self):
        self.write("README.md", "# moshi-cockpit\n\nPlain text, no ids, mail a@example.test.\n")
        self.write("tools/x.py", "SHA = '" + "06a" + "0" * 61 + "'\n")
        self.write("ignored.log", f"{WORD}\n")
        self.write(".gitignore", "*.log\n")
        self.assertEqual(self.run_guard(), (0, ""))

    def test_file_names_are_scanned_and_redacted(self):
        self.write(f"share/{WORD}/a.txt", f"fine\n{SHORT_ID}\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["share/***/a.txt:0: blocked-word", "share/***/a.txt:2: ocellio-id"])


class WordListTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_the_list_is_read_from_the_environment_lower_cased_with_comments(self):
        path = self.dir / "words.txt"
        path.write_text(f"# private\n{WORD.upper()}\n\n  {HOST}  \n")
        self.assertEqual(leak_check.load_words({"MOSHI_COCKPIT_BLOCKED_WORDS": str(path)}), WORDS)

    def test_the_default_list_sits_in_the_home_config(self):
        path = self.dir / ".config" / "moshi-cockpit-dev" / "blocked-words.txt"
        path.parent.mkdir(parents=True)
        path.write_text(f"{WORD}\n")
        self.assertEqual(leak_check.load_words({"HOME": str(self.dir)}), frozenset({WORD}))

    def test_a_missing_or_empty_list_fails_closed(self):
        empty = self.dir / "empty.txt"
        empty.write_text("# only a comment\n\n")
        for target in (self.dir / "missing.txt", empty):
            with self.subTest(target=target.name):
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()) as out:
                    code = leak_check.main([], env={"MOSHI_COCKPIT_BLOCKED_WORDS": str(target)}, cwd=self.dir)
                self.assertEqual(code, 2)
                self.assertEqual(err.getvalue(),
                                 "leak_check: private word list not found (set MOSHI_COCKPIT_BLOCKED_WORDS)\n")
                self.assertEqual(out.getvalue(), "")

    def test_the_repository_contains_no_word_list(self):
        source = TOOL.read_text()
        self.assertFalse(hasattr(leak_check, "BLOCKED_SHA256"))
        self.assertFalse(hasattr(leak_check, "BLOCKED"))
        self.assertNotRegex(source, r"[0-9a-f]{64}")  # no hash list either
        self.assertNotIn("--hash", source)


class PublicTextTest(unittest.TestCase):
    """`--text` and `--refs`: text that becomes public outside git, and branch or tag names."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_guard(self, argv, stdin=""):
        out = io.StringIO()
        with redirect_stdout(out):
            code = leak_check.main(argv, words=WORDS, stdin=io.StringIO(stdin))
        return code, out.getvalue()

    def test_text_mode_flags_a_blocked_word_in_a_pr_body_and_prints_only_the_rule(self):
        body = self.dir / "body.md"
        body.write_text(f"## Summary\nfine\nWorks with {WORD} now.\nMail {PRIVATE_EMAIL}.\n")
        code, out = self.run_guard(["--text", str(body)])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["body.md:3: blocked-word", "body.md:4: email"])
        self.assertNotIn(WORD, out.lower())

    def test_text_mode_passes_clean_text_and_reads_stdin(self):
        clean = self.dir / "ok.md"
        clean.write_text("Plain text.\n")
        self.assertEqual(self.run_guard(["--text", str(clean)]), (0, ""))
        code, out = self.run_guard(["--text", "-"], stdin=f"fine\n{WORD}\n")
        self.assertEqual((code, out), (1, "-:2: blocked-word\n"))

    def test_text_mode_redacts_a_path_that_trips_a_rule(self):
        path = self.dir / f"{WORD}.md"
        path.write_text(f"{SHORT_ID}\n")
        code, out = self.run_guard(["--text", str(path)])
        self.assertEqual((code, out), (1, "***:0: blocked-word\n***:1: ocellio-id\n"))

    def test_refs_mode_flags_a_blocked_branch_name(self):
        code, out = self.run_guard(["--refs", f"feat/{WORD}-fix", "feat/clean"])
        self.assertEqual((code, out), (1, "ref: blocked-word\n"))
        self.assertEqual(self.run_guard(["--refs", "feat/clean", "v1.0.0"]), (0, ""))

    def test_text_mode_survives_bytes_that_are_not_utf8(self):
        stdin = io.TextIOWrapper(io.BytesIO(b"fine\n\xff\xfe " + WORD.encode() + b"\n"), encoding="ascii")
        out = io.StringIO()
        with redirect_stdout(out):
            code = leak_check.main(["--text", "-"], words=WORDS, stdin=stdin)
        self.assertEqual((code, out.getvalue()), (1, "-:2: blocked-word\n"))

    def test_the_modes_exclude_each_other(self):
        for argv in (["--text", "-", "--refs", "a"], ["--commits", "HEAD", "--refs", "a"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                leak_check.main(argv, words=WORDS)
            self.assertEqual(raised.exception.code, 2)

    def test_text_and_refs_fail_closed_without_the_word_list(self):
        for argv in (["--text", "-"], ["--refs", "main"]):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = leak_check.main(argv, env={"MOSHI_COCKPIT_BLOCKED_WORDS": str(self.dir / "none")},
                                       stdin=io.StringIO(""))
            self.assertEqual((code, err.getvalue()), (2, leak_check.NO_WORDS + "\n"))


class PrePushHookTest(unittest.TestCase):
    HOOK = TOOL.parent / "hooks" / "pre-push"

    def push(self, local: str, remote: str, newline: str = "\n", words_text: str = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        words = Path(tmp.name) / "words.txt"
        words.write_text(f"{WORD}\n" if words_text is None else words_text)
        env = dict(os.environ, MOSHI_COCKPIT_BLOCKED_WORDS=str(words))
        line = f"{local} {'a' * 40} {remote} {'0' * 40}{newline}"
        return subprocess.run(["sh", str(self.HOOK), "origin", "url"], input=line, env=env,
                              capture_output=True, text=True, cwd=TOOL.parent.parent)

    def test_pre_push_refuses_a_branch_with_a_blocked_name(self):
        for local, remote in ((f"refs/heads/{WORD}-x", "refs/heads/ok"), ("refs/heads/ok", f"refs/tags/{WORD}")):
            with self.subTest(local=local.split("/")[-1][:4]):
                done = self.push(local, remote)
                self.assertEqual(done.returncode, 1)
                self.assertIn("ref: blocked-word", done.stdout)
                self.assertNotIn(WORD, done.stdout + done.stderr)
                self.assertNotIn("== ", done.stdout)  # refused before the full check ran

    def test_pre_push_checks_a_last_line_without_a_newline(self):
        done = self.push(f"refs/heads/{WORD}", "refs/heads/ok", newline="")
        self.assertEqual((done.returncode, "ref: blocked-word" in done.stdout), (1, True))

    def test_pre_push_passes_a_ref_that_starts_with_a_dash_as_a_name(self):
        done = self.push(f"refs/heads/-{WORD}", "refs/heads/ok")
        self.assertEqual(done.returncode, 1)
        self.assertIn("ref: blocked-word", done.stdout)
        self.assertNotIn("usage", done.stderr)

    def test_pre_push_without_the_word_list_stops_before_the_full_check(self):
        done = self.push("refs/heads/ok", "refs/heads/ok", words_text="# none\n")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("== ", done.stdout)


if __name__ == "__main__":
    unittest.main()
