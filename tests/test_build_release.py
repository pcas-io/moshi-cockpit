"""The release installer: tools/build_release.py renders install.sh, and the rendered script runs against a local
HTTP server (no network) with a temp HOME. Python and the payload are small stand-ins whose hashes the test
renders into the script, so a changed byte on the server is a tampered download."""

from __future__ import annotations

import fcntl
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import termios
import threading
import unittest
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
build_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_release)

VERSION = "0.0.1"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UNSUPPORTED = "Windows follows; this release supports macOS on Apple silicon and Linux on x86_64 and aarch64."
PYTHON_STUB = "#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n"
# Stands in for the real bin/moshi-cockpit: records how the installer called it.
COCKPIT_STUB = ("#!/bin/sh\n"
                "printf '%s\\n' \"$0\" \"$@\" > \"$HOME/cockpit-called\"\n"
                "if [ -t 0 ]; then echo tty >> \"$HOME/cockpit-called\"; fi\n")


def tarball(files: Dict[str, str]) -> bytes:
    """A .tar.gz holding executable text files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Server:
    """Serves a directory over HTTP on 127.0.0.1 and remembers every path asked for."""

    def __init__(self, root: Path) -> None:
        requests: List[str] = []

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def do_GET(self):
                requests.append(self.path)
                super().do_GET()

            def log_message(self, *_args):
                pass

        self.requests = requests
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class BuildTest(unittest.TestCase):
    def test_build_renders_install_sh_with_the_payload_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / f"moshi-cockpit-{VERSION}.tar.gz"
            payload.write_bytes(b"payload bytes")
            out = Path(tmp) / "install.sh"
            done = subprocess.run(["python3", str(ROOT / "tools" / "build_release.py"), "--version", VERSION,
                                   "--payload", str(payload), "--out", str(out)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            script = out.read_text()
        self.assertIn(f"PAYLOAD_SHA256='{sha256(b'payload bytes')}'", script)
        self.assertIn(f"VERSION='{VERSION}'", script)
        for key, (url, sha) in build_release.PYTHONS.items():
            name = key.upper().replace("-", "_")
            self.assertIn(f"PYTHON_URL_{name}='{url}'", script)
            self.assertIn(f"PYTHON_SHA256_{name}='{sha}'", script)
        self.assertIn(f"RELEASE_URL='https://github.com/pcas-io/moshi-cockpit/releases/download/v{VERSION}'", script)
        self.assertIn("CURL_PROTO='=https'", script)
        self.assertIn("HERDR_APP='/Applications/Herdr.app'", script)
        self.assertNotRegex(script, r"@[A-Z0-9_]+@")

    def test_the_three_pinned_runtimes_are_astral_3_12_16(self):
        self.assertEqual(build_release.PYTHON_VERSION, "3.12.16")
        pins = build_release.PYTHONS
        self.assertEqual(sorted(pins), ["linux-aarch64", "linux-x86_64", "macos-arm64"])
        self.assertEqual(pins["macos-arm64"][1], "fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e")
        self.assertEqual(pins["linux-x86_64"][1], "6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de")
        self.assertEqual(pins["linux-aarch64"][1], "a945427411dc6851a1393533b74a4a1ac434321ae397f2ec69897a9b4fde032b")
        triples = {"macos-arm64": "aarch64-apple-darwin", "linux-x86_64": "x86_64-unknown-linux-gnu",
                   "linux-aarch64": "aarch64-unknown-linux-gnu"}
        for key, (url, _sha) in pins.items():
            with self.subTest(key=key):
                self.assertTrue(url.startswith(
                    "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"))
                self.assertIn("20261026", url)
                self.assertIn("3.12.16", url)
                self.assertTrue(url.endswith(f"cpython-3.12.16%2B20261026-{triples[key]}-install_only.tar.gz"))

    def test_bad_inputs_are_refused(self):
        good = "a" * 64
        for version, sha in (("1.2", good), ("v1.2.3", good), ("1.2.3", "a" * 63), ("1.2.3", "A" * 64),
                             ("1.2.3'; rm -rf ~; '", good)):
            with self.subTest(version=version, sha=sha), self.assertRaises(ValueError):
                build_release.render(version, sha)
        with self.assertRaises(ValueError):
            build_release.render("1.2.3", good, release_url="https://x.example.test/it's")
        pins = dict(build_release.PYTHONS)
        for pythons in ({k: v for k, v in pins.items() if k != "linux-aarch64"},
                        {**pins, "linux-armv7": pins["linux-x86_64"]},
                        {**pins, "linux-x86_64": (pins["linux-x86_64"][0], "a" * 63)},
                        {**pins, "linux-aarch64": ("https://x.example.test/it's", good)}):
            with self.subTest(pythons=sorted(pythons)), self.assertRaises(ValueError):
                build_release.render("1.2.3", good, pythons=pythons)

    def test_the_rendered_script_passes_shellcheck(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "install.sh"
            out.write_text(build_release.render(VERSION, "b" * 64))
            done = subprocess.run(["shellcheck", "-s", "sh", str(out)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_the_template_refuses_to_run_unrendered(self):
        done = subprocess.run(["sh", str(ROOT / "install.sh")], capture_output=True, text=True,
                              env={"PATH": SYSTEM_PATH, "HOME": "/nonexistent"}, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 1)
        self.assertIn("releases/latest/download/install.sh", done.stderr)


class InstallerRunTest(unittest.TestCase):
    """The rendered script, piped into sh like `curl … | sh`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.web = self.tmp / "web"
        (self.web / "release").mkdir(parents=True)
        self.herdr = self.tmp / "Applications" / "Herdr.app"
        self.herdr.mkdir(parents=True)
        self.python = tarball({"python/bin/python3": PYTHON_STUB})
        self.payload = tarball({f"moshi-cockpit-{VERSION}/bin/moshi-cockpit": COCKPIT_STUB,
                                f"moshi-cockpit-{VERSION}/cockpit/__init__.py": ""})
        self.server = Server(self.web)
        self.addCleanup(self.server.close)
        self.fakes = self.tmp / "fakes"
        self.fakes.mkdir()

    def serve(self, python: Optional[bytes] = None, payload: Optional[bytes] = None) -> None:
        for key in build_release.PYTHONS:
            (self.web / f"cpython-{key}.tar.gz").write_bytes(self.python if python is None else python)
        (self.web / "release" / f"moshi-cockpit-{VERSION}.tar.gz").write_bytes(
            self.payload if payload is None else payload)

    def script(self, herdr: Optional[Path] = None) -> str:
        return build_release.render(
            VERSION, sha256(self.payload),
            pythons={k: (f"{self.server.url}/cpython-{k}.tar.gz", sha256(self.python)) for k in build_release.PYTHONS},
            release_url=f"{self.server.url}/release",
            herdr_app=str(herdr or self.herdr), curl_proto="=http")

    def run_installer(self, *args: str, herdr: Optional[Path] = None, **kwargs) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.fakes}:{SYSTEM_PATH}", "HOME": str(self.home)}
        return subprocess.run(["sh", "-s", "--", *args], input=self.script(herdr), capture_output=True, text=True,
                              env=env, start_new_session=True, timeout=60, **kwargs)  # new session: no /dev/tty

    @property
    def data(self) -> Path:
        return self.home / ".local" / "share" / "moshi-cockpit"

    def called(self) -> List[str]:
        return (self.home / "cockpit-called").read_text().splitlines()

    def uname(self, system: str, machine: str) -> None:
        """A fake `uname` first on the PATH: -s prints the system, anything else the machine."""
        fake = self.fakes / "uname"
        fake.write_text(f"#!/bin/sh\ncase \"$1\" in -s) echo {system} ;; *) echo {machine} ;; esac\n")
        fake.chmod(0o755)

    def test_installs_the_runtime_and_the_app_then_runs_install(self):
        self.serve()
        done = self.run_installer("--moshi", "https://moshi.example.test/mcp",
                                  "--ocellio", "https://ocellio.example.test/mcp/")
        self.assertEqual(done.returncode, 0, done.stderr)
        app = self.data / "app" / VERSION
        self.assertTrue((self.data / "runtime" / "python" / "bin" / "python3").is_file())
        self.assertEqual(self.called(), [str(app / "bin" / "moshi-cockpit"), "install",
                                         "--moshi", "https://moshi.example.test/mcp",
                                         "--ocellio", "https://ocellio.example.test/mcp/"])
        self.assertEqual(sorted(self.server.requests),
                         ["/cpython-macos-arm64.tar.gz", f"/release/moshi-cockpit-{VERSION}.tar.gz"])

    def test_without_flags_and_without_a_terminal_it_asks_nothing(self):
        self.serve()
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.called()[1:], ["install"])
        self.assertNotIn("address", done.stdout + done.stderr)

    def test_a_second_run_keeps_the_verified_runtime(self):
        self.serve()
        self.assertEqual(self.run_installer().returncode, 0)
        self.server.requests.clear()
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.server.requests, [f"/release/moshi-cockpit-{VERSION}.tar.gz"])

    def test_a_runtime_that_does_not_start_is_downloaded_again(self):
        # a musl host, say: the glibc build unpacks but does not start. No marker, so the next run fetches it again.
        good = self.python
        self.python = tarball({"python/bin/python3": "#!/bin/sh\nexit 1\n"})
        self.serve()
        done = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertIn("the bundled Python does not start", done.stderr)
        self.assertFalse((self.data / "runtime" / ".sha256").exists())
        self.assertFalse((self.home / "cockpit-called").exists())
        self.python = good
        self.serve()
        self.server.requests.clear()
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("/cpython-macos-arm64.tar.gz", self.server.requests)
        self.assertEqual((self.data / "runtime" / ".sha256").read_text().strip(), sha256(good))

    def test_rendered_script_refuses_a_tampered_payload(self):
        self.serve(payload=self.payload + b"x")
        done = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertIn("checksum", done.stderr)
        self.assertIn("moshi-cockpit", done.stderr)
        self.assertFalse((self.data / "app").exists())
        self.assertFalse((self.data / "runtime").exists())  # nothing unpacked before both hashes are checked
        self.assertFalse((self.home / "cockpit-called").exists())

    def test_rendered_script_refuses_a_tampered_python(self):
        self.serve(python=tarball({"python/bin/python3": "#!/bin/sh\necho evil\n"}))
        for system, machine, key in (("Darwin", "arm64", "macos-arm64"), ("Linux", "x86_64", "linux-x86_64"),
                                     ("Linux", "aarch64", "linux-aarch64")):
            with self.subTest(key=key):
                self.uname(system, machine)
                self.server.requests.clear()
                done = self.run_installer()
                self.assertEqual(done.returncode, 1)
                self.assertIn("checksum", done.stderr)
                self.assertIn("Python", done.stderr)
                self.assertIn(f"/cpython-{key}.tar.gz", self.server.requests)
                self.assertFalse((self.data / "runtime").exists())
                self.assertFalse((self.data / "app").exists())
                self.assertFalse((self.home / "cockpit-called").exists())

    def test_rendered_script_stops_without_herdr(self):
        self.serve()
        done = self.run_installer(herdr=self.tmp / "Applications" / "Missing.app")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Install Herdr from https://herdr.dev first, then run this again.", done.stderr)
        self.assertEqual(self.server.requests, [])  # nothing downloaded
        self.assertFalse(self.data.exists())

    def test_linux_x86_64_installs_headless_without_herdr(self):
        self.uname("Linux", "x86_64")
        self.serve()
        done = self.run_installer(herdr=self.tmp / "Applications" / "Missing.app")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.server.requests),
                         ["/cpython-linux-x86_64.tar.gz", f"/release/moshi-cockpit-{VERSION}.tar.gz"])
        self.assertEqual(self.called(), [str(self.data / "app" / VERSION / "bin" / "moshi-cockpit"), "install"])
        self.assertNotIn("Herdr from", done.stderr)

    def test_linux_aarch64_picks_its_runtime(self):
        for machine in ("aarch64", "arm64"):
            with self.subTest(machine=machine):
                self.uname("Linux", machine)
                self.serve()
                shutil.rmtree(self.data, ignore_errors=True)
                self.server.requests.clear()
                done = self.run_installer(herdr=self.tmp / "Applications" / "Missing.app")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(sorted(self.server.requests),
                                 ["/cpython-linux-aarch64.tar.gz", f"/release/moshi-cockpit-{VERSION}.tar.gz"])

    def test_unknown_linux_architecture_is_refused_before_download(self):
        self.uname("Linux", "armv7l")
        self.serve()
        done = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertIn(UNSUPPORTED, done.stderr)
        self.assertEqual(self.server.requests, [])
        self.assertFalse(self.data.exists())

    def test_other_systems_are_told_what_follows(self):
        for system, machine in (("MINGW64_NT-10.0", "x86_64"), ("Darwin", "x86_64")):
            with self.subTest(system=system, machine=machine):
                self.uname(system, machine)
                self.serve()
                done = self.run_installer()
                self.assertEqual(done.returncode, 1)
                self.assertIn(UNSUPPORTED, done.stderr)
                self.assertEqual(self.server.requests, [])

    def test_checksum_uses_sha256sum_when_present(self):
        fake = self.fakes / "sha256sum"
        fake.write_text("#!/bin/sh\n"
                        "printf '%s\\n' \"$@\" >> \"$HOME/sha256sum-called\"\n"
                        "exec /usr/bin/python3 -I -c 'import hashlib, sys; "
                        "print(hashlib.sha256(open(sys.argv[1], \"rb\").read()).hexdigest() + \"  \" + sys.argv[1])' "
                        "\"$1\"\n")
        fake.chmod(0o755)
        self.serve()
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stderr)
        checked = (self.home / "sha256sum-called").read_text().splitlines()
        self.assertEqual(len(checked), 2)  # Python and the payload
        self.assertEqual(self.called()[1:], ["install"])
        self.serve(payload=self.payload + b"x")
        done = self.run_installer()
        self.assertEqual(done.returncode, 1)
        self.assertIn("checksum mismatch (moshi-cockpit)", done.stderr)

    def test_an_unknown_flag_stops_before_anything_happens(self):
        self.serve()
        done = self.run_installer("--moshii", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("--moshii", done.stderr)
        self.assertEqual(self.server.requests, [])

    def run_on_terminal(self, typed: bytes):
        """The rendered script with a pty as its controlling terminal, as a real shell would have; `typed` is
        already waiting there. Returns the process and what the script printed on the terminal."""
        self.serve()
        master, slave = os.openpty()
        self.addCleanup(os.close, master)

        def own_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        seen = bytearray()

        def drain():
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                seen.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        os.write(master, typed)
        env = {"PATH": SYSTEM_PATH, "HOME": str(self.home)}
        proc = subprocess.Popen(["sh", "-s"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, preexec_fn=own_terminal, pass_fds=(slave,))
        _, err = proc.communicate(self.script().encode(), timeout=60)
        os.close(slave)
        reader.join(timeout=5)
        self.assertEqual(proc.returncode, 0, err.decode())
        return seen.decode(errors="replace")

    def test_with_a_terminal_it_asks_for_both_addresses_on_dev_tty(self):
        prompts = self.run_on_terminal(b"https://moshi.example.test/mcp\n\n")  # moshi given, ocellio left empty
        self.assertIn("moshi address (leave empty to use Herdr without moshi):", prompts)
        self.assertIn("ocellio address (leave empty to skip):", prompts)
        self.assertEqual(self.called()[1:], ["install", "--moshi", "https://moshi.example.test/mcp", "tty"])

    def test_an_update_shows_the_current_addresses_and_keeps_them_on_empty_input(self):
        settings = self.home / ".config" / "moshi-cockpit"
        settings.mkdir(parents=True)
        (settings / "config.json").write_text(json.dumps({"servers": {
            "moshi": "https://moshi.example.test/mcp", "ocellio": "https://ocellio.example.test/mcp/\x1b[2J"}}))
        prompts = self.run_on_terminal(b"\n\n")
        self.assertIn("moshi address [current: https://moshi.example.test/mcp] (leave empty to keep it):", prompts)
        self.assertIn("ocellio address [current: https://ocellio.example.test/mcp/[2J] (leave empty to keep it):",
                      prompts)  # control characters from the file never reach the terminal
        self.assertNotIn("\x1b[2J", prompts)
        self.assertEqual(self.called()[1:], ["install", "tty"])  # no flag: install keeps both


if __name__ == "__main__":
    unittest.main()
