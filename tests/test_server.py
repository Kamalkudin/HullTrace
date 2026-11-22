import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from Maritim.server import Application, make_server


def fake_collect(imo, progress):
    progress('Example source: collected')
    return {'imo': imo, 'generated_at': '2026-11-19T00:00:00+00:00',
            'sources': {'demo': {'label': 'Example source', 'url': 'https://example.org', 'status': 'collected',
                                 'retrieved': '2026-11-19T00:00:00+00:00', 'body': 'Synthetic test only'}},
            'records': [{'section': 'Identity', 'label': 'Reported vessel name', 'value': 'TEST VESSEL', 'source': 'demo'}],
            'warnings': ['Synthetic test only.'], 'links': []}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(self.tmp.name, fake_collect)
        self.server = make_server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        headers = headers or {}
        if data is not None:
            headers.setdefault('Content-Type', 'application/json')
        payload = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.base + path, data=payload, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def wait(self, job):
        for _ in range(100):
            data = json.loads(self.request('/api/jobs/' + job)[2])
            if data['state'] != 'running':
                return data
            time.sleep(.02)
        self.fail('Test lookup did not finish')

    def test_page_and_assets(self):
        for path in ['/', '/app.js', '/style.css', '/favicon.svg', '/saved', '/records', '/records.js', '/qualifier', '/evidence/starship-source.jpg']:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_workspace_pages_have_distinct_controls(self):
        lookup = self.request('/')[2]
        self.assertNotIn(b'id="history-filter"', lookup)
        self.assertIn(b'id="history-filter"', self.request('/saved')[2])
        self.assertIn(b'href="/records"', lookup)
        records = self.request('/qualifier')[2]
        self.assertIn(b'id="records-file"', records)
        for old in (b'Maltego', b'Lost at Sea', b'id="clue"'):
            self.assertNotIn(old, records)

    def test_csv_import_byte_limit_and_utf8(self):
        headers = {'Content-Type': 'text/csv'}
        prefix = 'kind,imo,date,port,source\nport,9305556,2026-01-14,İzmir,https://example.org/'.encode()
        limit = 1024 * 1024
        body = prefix + b'a' * (limit - len(prefix))
        status, _, raw = self.request('/api/records', body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['records'][0]['port'], 'İzmir')
        self.assertEqual(self.request('/api/records', body + b'a', headers)[0], 413)
        for invalid in (b'', b'   ', b'\xff', b'kind,source\nport,nope'):
            self.assertEqual(self.request('/api/records', invalid, headers)[0], 400)

    def test_records_handle_all_kinds_without_challenge_clues(self):
        rows = ('kind,source,imo,date,port,condition,deficiency,container,vessel,month,mmsi\n'
                'port,https://example.org/p,9413432,2025-04-01,Test port,,,,,,\n'
                'inspection,https://example.org/i,9413432,2025-05-01,,Missing,Pump,,,,\n'
                'voyage,https://example.org/v,9413432,,,,,TEST1234567,Test vessel,,\n'
                'radio,https://example.org/r,9413432,,,,,,,2025-06,123456789\n')
        status, _, raw = self.request('/api/records', rows.encode('utf-8-sig'), {'Content-Type': 'text/csv'})
        self.assertEqual(status, 200)
        self.assertEqual([r['kind'] for r in json.loads(raw)['records']], ['port', 'inspection', 'voyage', 'radio'])

    def test_replay_original_maritime_clues(self):
        images = ['lost-at-sea-1.png', 'lost-at-sea-2.png', 'the-renamed-vessel.png']
        clues = [hashlib.sha256(self.request('/evidence/' + image)[2]).hexdigest() for image in images]
        clues.insert(2, 'tjm7w3')
        for assisted in (True, False):
            status, _, body = self.request('/api/qualifier', {'clues': clues, 'assisted': assisted})
            self.assertEqual(status, 200)
            results = [f for c in json.loads(body)['cases'] for f in c['findings']]
            expected = ['flag{SHIP_40}', 'flag{NORMAND_RANGER_9413432}']
            expected += ['flag{PORT_YARIMCA}', 'flag{EMERGENCY_FIRE_PUMP}', 'flag{367037568}'] if assisted else [None] * 3
            self.assertEqual([r['flag'] for r in results], expected)
            self.assertEqual([r['status'] for r in results], ['Source-backed'] * 2 +
                             ['Walkthrough-assisted' if assisted else 'Evidence needed'] * 3)

    def test_imported_history_is_scoped_and_conflicts_visible(self):
        header = 'kind,imo,date,port,source\n'
        rows = ('port,9305556,2026-01-15,Wrong day,https://example.org/a\n'
                'port,9243162,2026-01-14,Wrong hull,https://example.org/b\n'
                'port,9305556,2026-01-14,Test Port,https://example.org/c\n')
        data = {'clues': ['TJM7W3'], 'records': header + rows, 'assisted': False}
        def first():
            return json.loads(self.request('/api/qualifier', data)[2])['cases'][0]['findings'][0]
        self.assertEqual(first()['flag'], 'flag{PORT_TEST_PORT}')
        self.assertEqual(first()['status'], 'Investigator-supplied')
        data['assisted'] = True
        self.assertEqual(first()['status'], 'Conflicting records')
        self.assertIsNone(first()['flag'])

    def test_imported_container_to_historical_identity(self):
        records = ('kind,container,vessel,imo,month,mmsi,source\n'
                   'voyage,TEST1234567,Example vessel,9243162,,,https://example.org/voyage\n'
                   'radio,,,9243162,2026-11,123456789,https://example.org/later\n'
                   'radio,,,9243162,2013-06,987654321,https://example.org/earlier\n')
        data = {'clues': ['Check TEST1234567'], 'records': records, 'assisted': False}
        result = json.loads(self.request('/api/qualifier', data)[2])['cases'][0]['findings'][0]
        self.assertEqual(result['flag'], 'flag{987654321}')
        self.assertEqual(result['status'], 'Investigator-supplied')

    def test_unrecognized_clue_and_invalid_import(self):
        data = {'clues': ['unrelated'], 'assisted': True}
        self.assertFalse(json.loads(self.request('/api/qualifier', data)[2])['cases'][0]['findings'])
        data['records'] = 'kind,source\nport,javascript:alert(1)'
        self.assertEqual(self.request('/api/qualifier', data)[0], 400)

    def test_full_scan_history_and_download(self):
        status, _, body = self.request('/api/scans', {'imo': '1234567'})
        self.assertEqual(status, 202)
        job = self.wait(json.loads(body)['job_id'])
        self.assertEqual(job['state'], 'complete')
        report = json.loads(self.request('/api/reports/' + job['report_id'])[2])
        self.assertEqual(report['records'][0]['value'], 'TEST VESSEL')
        self.assertNotIn('body', report['sources']['demo'])
        self.assertEqual(len(report['challenge_leads']), 6)
        history = json.loads(self.request('/api/history')[2])
        self.assertEqual(history['reports'][0]['name'], 'TEST VESSEL')
        status, headers, body = self.request('/reports/' + job['report_id'] + '/data.json')
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])

    def test_invalid_imo_and_payload(self):
        for data in [{'imo':'1234560'}, {'imo':'abc'}, {'imo':1234567}, [], {}]:
            self.assertEqual(self.request('/api/scans', data)[0], 400)

    def test_cross_origin_request_rejected(self):
        self.assertEqual(self.request('/api/scans', {'imo':'1234567'}, {'Origin':'https://unrelated.example'})[0], 403)

    def test_untrusted_host_rejected(self):
        self.assertEqual(self.request('/api/history', headers={'Host':'unrelated.example'})[0], 403)

    def test_codespace_forwarding_supports_both_host_header_forms(self):
        env = {'CODESPACES': 'true', 'CODESPACE_NAME': 'example-space',
               'GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN': 'forward.example'}
        port = self.server.server_port
        host = f'example-space-{port}.forward.example'
        with patch.dict(os.environ, env):
            for forwarded_host in (host, f'localhost:{port}'):
                headers = {'Host': forwarded_host, 'Origin': 'https://' + host}
                self.assertEqual(self.request('/qualifier', headers=headers)[0], 200)
                status, _, body = self.request('/api/qualifier', {'clues': ['TJM7W3'], 'assisted': True}, headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['cases'][0]['findings'][0]['flag'], 'flag{PORT_YARIMCA}')

    def test_forwarding_is_limited_to_this_codespace_and_https_origin(self):
        env = {'CODESPACES': 'true', 'CODESPACE_NAME': 'example-space',
               'GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN': 'forward.example'}
        host = f'example-space-{self.server.server_port}.forward.example'
        data = {'clues': ['TJM7W3'], 'assisted': False}
        with patch.dict(os.environ, env):
            for headers in ({'Host': 'other-' + host}, {'Host': host, 'Origin': 'https://other-' + host},
                            {'Host': host, 'Origin': 'http://' + host},
                            {'Host': host, 'Origin': 'https://' + host, 'Sec-Fetch-Site': 'cross-site'}):
                self.assertEqual(self.request('/api/qualifier', data, headers)[0], 403)
        with patch.dict(os.environ, {**env, 'CODESPACES': 'false'}):
            self.assertEqual(self.request('/api/history', headers={'Host': host})[0], 403)

    def test_plain_form_request_rejected(self):
        self.assertEqual(self.request('/api/scans', {'imo':'1234567'}, {'Content-Type':'text/plain'})[0], 415)

    def test_report_paths_confined(self):
        for path in ['/api/reports/../../README', '/reports/1234567/../../README.md', '/reports/1234567/test/sources/demo.txt']:
            self.assertIn(self.request(path)[0], {400,404})

    def test_failed_job_is_reported_and_does_not_block_next_job(self):
        def fail(imo, progress):
            raise OSError('Test network failure')
        self.app.collect = fail
        job = self.wait(self.app.start('1234567'))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('network', job['error'])
        self.app.collect = fake_collect
        self.assertEqual(self.wait(self.app.start('1234567'))['state'], 'complete')

    def test_only_one_lookup_runs_at_a_time(self):
        release = threading.Event()
        def slow(imo, progress):
            release.wait(3)
            return fake_collect(imo, progress)
        self.app.collect = slow
        job_id = self.app.start('1234567')
        try:
            self.assertEqual(self.request('/api/scans', {'imo':'1234567'})[0], 409)
        finally:
            release.set()
            self.wait(job_id)


if __name__ == '__main__':
    unittest.main()
