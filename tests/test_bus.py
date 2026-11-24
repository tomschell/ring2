"""Which bus messages a role receives. Run: python3 -m unittest tests.test_bus"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ring2 import Refused, app, busd
from ring2.bus import addressed


class StatusTextTest(unittest.TestCase):
    def line(self, **row):
        return app.status_text([{'label': 'ann', 'session': True, 'loop': True, **row}])

    def test_the_line_says_whether_a_message_waits_not_two_sequence_numbers(self):
        # The cursor is at 14 and the newest message naming the role is 12: nothing waits.
        self.assertTrue(self.line(delivered=14, addressed=12).endswith(', waiting: no'))
        self.assertTrue(self.line(delivered=12, addressed=12).endswith(', waiting: no'))
        self.assertTrue(self.line(delivered=12, addressed=14).endswith(', waiting: yes'))
        self.assertTrue(self.line().endswith(', waiting: no'))   # no bus, or no cursor yet
        self.assertNotIn('14', self.line(delivered=12, addressed=14))


class PostAddressTest(unittest.TestCase):
    def test_leading_role_requires_at_sign(self):
        cfg = {'roles': {'imp': {}, 'rev': {}}}
        with patch.object(app, 'load', return_value=cfg), patch.object(app.bus, 'post') as post:
            with self.assertRaisesRegex(Refused, '^ring2: address a role as @rev$'):
                app.main(['--as', 'imp', 'post', 'team', 'rev: please review'])
            post.assert_not_called()
            with patch('builtins.print'):
                post.return_value = {}
                app.main(['--as', 'imp', 'post', 'team', '@rev: please review'])
            post.assert_called_once_with(cfg, 'team', 'imp: @rev: please review')


class AddressedTest(unittest.TestCase):
    def test_exact_label_or_all(self):
        self.assertTrue(addressed('exp-role', '@exp-role please look'))
        self.assertTrue(addressed('exp-role', 'cc @exp-role.'))
        self.assertTrue(addressed('exp-role', '@all stop'))

    def test_other_labels_and_lookalikes(self):
        self.assertFalse(addressed('exp-role', '@other-role please look'))
        self.assertFalse(addressed('exp-role', '@exp-role-2 please look'))
        self.assertFalse(addressed('exp-role', 'mail me at x@exp-role'))
        self.assertFalse(addressed('exp-role', 'exp-role without a mention'))


class BrowserRequestTest(unittest.TestCase):
    def post(self, content_type='application/json', host=None):
        with tempfile.TemporaryDirectory() as directory:
            server = busd.RingServer(('127.0.0.1', 0), busd.RingApp(Path(directory) / 'bus.db'))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                headers = {'Content-Type': content_type}
                conn.putrequest('POST', '/channels/team', skip_host=host is not None)
                if host is not None:
                    conn.putheader('Host', host)
                for name, value in headers.items():
                    conn.putheader(name, value)
                body = b'{"message":"ann: hi"}'
                conn.putheader('Content-Length', str(len(body)))
                conn.endheaders(body)
                status = conn.getresponse().status
                conn.close()
                return status
            finally:
                server.shutdown()
                server.server_close()

    def test_normal_post_works(self):
        self.assertEqual(self.post(), 201)
        self.assertEqual(self.post(host='localhost:8770'), 201)
        self.assertEqual(self.post(host='[::1]:8770'), 201)

    def test_text_plain_post_is_refused(self):
        self.assertEqual(self.post('text/plain'), 415)

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.post(host='evil.example'), 403)
        self.assertEqual(self.post(host='evil.example:8770'), 403)
        self.assertEqual(self.post(host='[::1].evil.example'), 403)
        self.assertEqual(self.post(host='localhost.evil.example:8770'), 403)


if __name__ == '__main__':
    unittest.main()
