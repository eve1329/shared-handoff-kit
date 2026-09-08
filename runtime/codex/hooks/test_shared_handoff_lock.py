#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

import shared_handoff_lock


class SharedHandoffLockTests(unittest.TestCase):
    def test_posix_backend_locks_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "session-tasks.json.lock"
            with shared_handoff_lock.exclusive_file_lock(path):
                self.assertTrue(path.exists())

    def test_windows_backend_acquires_and_releases_the_same_byte(self) -> None:
        calls: list[tuple[int, int]] = []
        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=lambda _fd, mode, length: calls.append((mode, length)),
        )
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "session-tasks.json.lock"
            with mock.patch.object(shared_handoff_lock.os, "name", "nt"):
                with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
                    with shared_handoff_lock.exclusive_file_lock(path):
                        self.assertEqual(path.read_text(encoding="utf-8"), "0")

            self.assertEqual(calls, [(fake_msvcrt.LK_NBLCK, 1), (fake_msvcrt.LK_UNLCK, 1)])


if __name__ == "__main__":
    unittest.main()
