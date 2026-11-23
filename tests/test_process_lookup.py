"""Process lookup keeps session tokens beyond a narrow terminal width."""
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from ring2 import owned_processes, session, write_handle


class ProcessLookupTest(unittest.TestCase):
    def test_long_command_with_columns_80(self):
        token = uuid.uuid4().hex
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)',
                                  'padding-' + 'x' * 256, token])
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'COLUMNS': '80'}):
                settings = {'state_dir': directory}
                write_handle(settings, {'name': 'worker', 'kind': 'test', 'token': token, 'pid': child.pid})
                self.assertIn(child.pid, owned_processes([token]))
                self.assertTrue(session.running(settings, 'worker', 'test'))
        finally:
            child.terminate()
            child.wait(timeout=5)
