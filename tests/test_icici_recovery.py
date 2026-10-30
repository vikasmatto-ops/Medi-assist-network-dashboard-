"""Regression tests for ICICI public source transient failures and duplicate city labels."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import refresh_icici
from icici_live_collector import ICICISession, URL
import requests


class FakeCitySession:
    def __init__(self, cities):
        self.cities = cities
        self.selected_state = None
        self.search_values = []

    def send(self, action, params=None):
        assert action == 'list_states'
        return '<select name="State">' + ''.join(
            f'<option value="S{n:02d}">S{n:02d}</option>' for n in range(30)
        ) + '</select>'

    def prepare_state(self, state):
        self.selected_state = state
        return {'label': state, 'value': state}, self.cities

    def search(self, state_value, city_value):
        self.search_values.append(city_value)
        return f'''<table><tr><th>Hospital Name</th><th>Address</th>
          <th>City</th><th>State</th><th>Ref No</th>
          <th>Hospital Type</th><th>Deposit Waiver</th></tr>
          <tr><td>Hospital {city_value}</td><td>Main Road</td>
          <td>One</td><td>{state_value}</td><td>{city_value}</td>
          <td>General</td><td>No</td></tr></table>'''


class RecoveryTests(unittest.TestCase):
    def test_distinct_source_values_with_identical_city_labels_are_both_scanned(self):
        client = FakeCitySession([{'label': 'One', 'value': 'c1'},
                                  {'label': 'One', 'value': 'c2'}])
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / 'shard.json'
            rc = refresh_icici.run_shard(0, 30, file, session=client)
            doc = json.loads(file.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(client.search_values, ['c1', 'c2'])
        self.assertTrue(doc['complete'])
        self.assertEqual(doc['states'][0]['repeated_city_labels'], 1)
        self.assertEqual(len(doc['states'][0]['cities']), 2)
        self.assertTrue(all(x['status'] == 'CAPTURED' for x in doc['states'][0]['cities']))

    def test_exact_duplicate_option_is_not_misrepresented_as_distinct(self):
        client = FakeCitySession([{'label': 'One', 'value': 'c1'},
                                  {'label': 'One', 'value': 'c1'}])
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / 'shard.json'
            rc = refresh_icici.run_shard(0, 30, file, session=client)
            doc = json.loads(file.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(client.search_values, ['c1'])
        self.assertEqual(doc['states'][0]['duplicate_exact_options_skipped'], 1)

    def test_conflicting_labels_for_same_value_fail_closed(self):
        client = FakeCitySession([{'label': 'One', 'value': 'c1'},
                                  {'label': 'Other', 'value': 'c1'}])
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / 'shard.json'
            rc = refresh_icici.run_shard(0, 30, file, session=client)
            doc = json.loads(file.read_text())
        self.assertEqual(rc, 2)
        self.assertEqual(client.search_values, [])
        self.assertIn('Conflicting city labels', doc['states'][0]['error'])

    def test_transient_connection_reset_retries_once_then_succeeds(self):
        client = ICICISession(pause=0, timeout=1)
        valid = SimpleNamespace(status_code=200, url=URL,
                                content=b'<html>okay</html>', text='<html>okay</html>',
                                raise_for_status=lambda: None)
        calls = [requests.exceptions.ConnectionError('closed'), valid]
        def get(*args, **kwargs):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(client.s, 'get', side_effect=get) as transport, patch('icici_live_collector.time.sleep'):
            result = client.send('list_states')
        self.assertEqual(result, '<html>okay</html>')
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(len(client.steps), 1)

    def test_access_denied_is_not_retried(self):
        client = ICICISession(pause=0, timeout=1)
        def forbidden():
            raise requests.exceptions.HTTPError('403 Forbidden')
        response = SimpleNamespace(status_code=403, url=URL,
                                   content=b'', text='', raise_for_status=forbidden)
        with patch.object(client.s, 'get', return_value=response) as transport, patch('icici_live_collector.time.sleep'):
            with self.assertRaises(requests.exceptions.HTTPError):
                client.send('list_states')
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(client.steps), 0)


if __name__ == '__main__':
    unittest.main()
