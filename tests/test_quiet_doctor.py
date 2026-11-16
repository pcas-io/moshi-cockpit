"""The silent doctor: nothing while all is well, one action on an error."""

import unittest
from unittest import mock

from cockpit import cli, doctor
from cockpit.doctor import FAIL, OK, WARN, Check
from tests.test_agents_doctor import context


def message(name, detail="x"):
    return doctor.quiet([Check(name, FAIL, detail)])


class QuietTest(unittest.TestCase):
    def test_all_well_says_nothing(self):
        self.assertIsNone(doctor.quiet([Check("a", OK, "gut")]))
        self.assertIsNone(doctor.quiet([]))

    def test_a_warning_stays_silent(self):
        self.assertIsNone(doctor.quiet([Check("herdr-app", WARN, "neuer als der Pin")]))

    def test_only_the_first_error_is_named(self):
        text = doctor.quiet([Check("a", OK, "gut"), Check("telemetry", FAIL, "an"), Check("endpoint-moshi", FAIL, "weg")])
        self.assertIn("Telemetrie", text)
        self.assertNotIn("antwortet", text)  # the second error (moshi endpoint) is not named

    def test_each_error_gets_its_action(self):
        for name, detail, expected in (
                ("herdr-core", "nicht installiert (Pin 1)", "moshi-cockpit install"),
                ("herdr-app", "1.0 installiert, aber Signatur falsch", "Entferne es"),
                ("telemetry", "an", "moshi-cockpit install"),
                ("permissions", "/x ist für Gruppe/andere schreibbar", "chmod go-w"),
                ("permissions", "/x ist kein Socket", "Prüfe diesen Pfad"),
                ("config", "kaputt", "Korrigiere"),
                ("endpoint-ocellio", "https://x nicht erreichbar (Zeitüberschreitung)", "Internetverbindung"),
                ("endpoint-ocellio", "https://x antwortet 500 (erwartet 401 + Bearer)", "Server-Adresse"),
                ("agent-research", "Keychain-Eintrag fehlt: moshi-research", "moshi-cockpit bot add research"),
                ("agent-research", "ocellio-Token noch an keinen Server gebunden", "moshi-cockpit bot add research"),
                ("agent-research", "Origin-Mismatch moshi: Token gebunden an a", "--rebind"),
                ("agent-research", "codex nicht installiert", "Installiere die CLI"),
                ("agent-research", "/x ist nicht 0600 (enthält Tokens)", "chmod 600"),
                ("agent-research", "Vibe-Profil /x weicht von dem ab, was moshi-cockpit schreibt", "moshi-cockpit install"),
                ("agent-research", "/x fehlt (moshi-cockpit install)", "moshi-cockpit install"),
                ("agent-research", "yolo ist gesetzt", "yolo ist gesetzt"),
                ("identity-register", "gebunden an x: moshi-cockpit bot register-token set --rebind", "--rebind")):
            with self.subTest(name=name, detail=detail):
                self.assertIn(expected, message(name, detail))

    def test_control_characters_never_reach_the_panel(self):
        self.assertNotIn("\x1b", message("identity-x", "böse\x1b[2J"))


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.ctx, _ = context(self)

    def doctor_cli(self, results, **options):
        args = mock.Mock(json=False, quiet=False)
        args.configure_mock(**options)
        with mock.patch.object(cli.doctor, "run", return_value=results), mock.patch("builtins.print") as printed:
            code = cli.cmd_doctor(args, self.ctx.paths)
        return code, [c.args[0] for c in printed.call_args_list]

    def test_quiet_prints_nothing_and_exits_zero_when_well(self):
        self.assertEqual(self.doctor_cli([Check("a", OK, "gut")], quiet=True), (0, []))

    def test_quiet_prints_one_line_and_fails_on_an_error(self):
        code, lines = self.doctor_cli([Check("telemetry", FAIL, "an")], quiet=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)

    def test_the_full_doctor_is_unchanged(self):
        _, lines = self.doctor_cli([Check("a", OK, "gut")])
        self.assertIn("1 Prüfungen", lines[0])

    def install(self, results, notes=("x",), **options):
        args = mock.Mock(skip_herdr=False, moshi=None, ocellio=None)
        args.configure_mock(**options)
        with mock.patch.object(cli, "_context", return_value=self.ctx), mock.patch.object(cli.installer, "run", return_value=list(notes)), \
                mock.patch.object(cli.doctor, "run", return_value=results) as run, mock.patch("builtins.print") as printed:
            code = cli.cmd_install(args, self.ctx.paths)
        return code, " ".join(str(c.args[0]) for c in printed.call_args_list), run

    def test_install_ends_quietly_when_all_is_well(self):
        code, text, run = self.install([Check("a", OK, "gut")])
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertIn("Fertig", text)
        self.assertNotIn("doctor", text)

    def test_install_names_one_action_on_an_error(self):
        code, text, _ = self.install([Check("telemetry", FAIL, "an")])
        self.assertEqual(code, 0)  # the install itself went through, and the release installer still shows its PATH hint
        self.assertIn("moshi-cockpit install", text)
        self.assertNotIn("Fertig", text)
        self.assertNotIn("!", text)  # in doctor's own language that would mean a mere hint

    def test_skip_herdr_does_not_complain_about_a_missing_herdr(self):
        _, text, run = self.install([Check("herdr-app", FAIL, "nicht installiert")], skip_herdr=True)
        run.assert_not_called()
        self.assertIn("Fertig", text)

    def test_a_herdr_timeout_keeps_its_own_last_sentence(self):
        from cockpit import installer
        _, text, run = self.install([], notes=[installer.HERDR_NOT_READY])
        run.assert_not_called()
        self.assertNotIn("Fertig", text)


if __name__ == "__main__":
    unittest.main()
