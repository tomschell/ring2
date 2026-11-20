"""The delivery cursor survives a cut while it is being replaced. No Codex, bus or tracker.

Run: python3 -m unittest tests.test_cursor
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ring2 import delivery


class Cut(BaseException):
    """The process dies here."""


class Session:
    thread = 'thread'

    def own(self):
        pass

    def send(self, text):
        return 'started'

    def started(self, marker):
        return True


class CursorTest(unittest.TestCase):
    def test_a_cut_at_replacement_leaves_the_old_cursor_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor = Path(tmp) / 'role.cursor'
            cursor.write_text('41')
            replaced = []

            def cut(source, target):
                replaced.append(Path(target))
                raise Cut()

            polls = []

            def source(after, wait):
                polls.append(after)
                if len(polls) > 1:
                    raise Cut()  # only reached if the cursor write did not go through os.replace
                return [{'sequence': 42, 'channel': 'c', 'message': '@x hello'}]

            with mock.patch.object(os, 'replace', cut), self.assertRaises(Cut):
                delivery.follow(source, lambda message: True, Session(), cursor, log=lambda line: None)
            self.assertEqual(replaced, [cursor], 'the cursor must be replaced by rename, never rewritten in place')
            self.assertEqual(cursor.read_text(), '41')
            self.assertEqual(delivery.init_cursor(None, cursor), 41)  # the next loop starts where it should

    def test_a_first_cursor_is_written_whole_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor = Path(tmp) / 'state' / 'role.cursor'
            with mock.patch.object(os, 'replace', mock.Mock(side_effect=Cut())), self.assertRaises(Cut):
                delivery.init_cursor(lambda after, wait: [{'sequence': 7}], cursor)
            self.assertFalse(cursor.exists(), 'no empty or partial cursor is left behind')
            self.assertEqual(delivery.init_cursor(lambda after, wait: [{'sequence': 7}], cursor), 7)


if __name__ == '__main__':
    unittest.main()
