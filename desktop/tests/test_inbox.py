"""Unit tests: the user -> Claude message queue (no windows)."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from desktop.inbox import Inbox, format_for_agent


class TestInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "inbox.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_take_marks_read_and_journals(self):
        ib = Inbox(self.path)
        seen = []
        ib.on_change = seen.append
        ib.add("hola")
        ib.add("segundo")
        self.assertEqual(ib.count(), 2)
        msgs = ib.take()
        self.assertEqual([m["text"] for m in msgs], ["hola", "segundo"])
        self.assertEqual(ib.count(), 0)
        self.assertEqual(ib.take(), [])
        self.assertEqual(seen, [1, 2, 0])
        events = [json.loads(line)["event"] for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["message", "message", "delivered"])

    def test_wait_returns_when_a_message_arrives(self):
        ib = Inbox(self.path)
        threading.Timer(0.3, lambda: ib.add("ya")).start()
        t0 = time.monotonic()
        msgs = ib.wait(5)
        self.assertEqual([m["text"] for m in msgs], ["ya"])
        self.assertLess(time.monotonic() - t0, 2)

    def test_wait_times_out_empty(self):
        self.assertEqual(Inbox(self.path).wait(0.2), [])

    def test_format_for_agent(self):
        txt = format_for_agent([{"id": 1, "at": "2026-01-01T10:00:00", "text": "para"}])
        self.assertIn("MESSAGE FROM THE USER", txt)
        self.assertIn("para", txt)


if __name__ == "__main__":
    unittest.main()
