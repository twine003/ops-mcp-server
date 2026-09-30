"""Unit tests: allow/deny policy (no Win32 calls)."""

import unittest

from desktop.safety import Policy, PolicyError

DENY = {"deny_processes": ["keepass.exe", "consent.exe"],
        "deny_window_title_regex": ["(?i)password|banque"]}


class TestPolicy(unittest.TestCase):
    def test_wildcard_allows_any_process(self):
        p = Policy({"allow_write_processes": ["*"], **DENY})
        for exe in ("msedge.exe", "EXCEL.EXE", "notepad.exe"):
            self.assertTrue(p.write_allowed(exe, "YouTube Studio"))
            p.check_write(exe, "YouTube Studio")

    def test_deny_wins_over_wildcard(self):
        p = Policy({"allow_write_processes": ["*"], **DENY})
        self.assertFalse(p.write_allowed("keepass.exe"))
        self.assertFalse(p.write_allowed("msedge.exe", "Banque Nationale - Connexion"))
        with self.assertRaises(PolicyError):
            p.check_write("consent.exe")
        with self.assertRaises(PolicyError):
            p.check_read("msedge.exe", "Change your password")

    def test_explicit_list_restricts(self):
        p = Policy({"allow_write_processes": ["mspaint.exe"], **DENY})
        self.assertTrue(p.write_allowed("MSPAINT.EXE"))
        self.assertFalse(p.write_allowed("msedge.exe"))
        with self.assertRaises(PolicyError):
            p.check_write("msedge.exe")

    def test_unknown_process_never_writable(self):
        p = Policy({"allow_write_processes": ["*"], **DENY})
        self.assertFalse(p.write_allowed(""))


if __name__ == "__main__":
    unittest.main()
