import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Maritim import vessel_lookup
from Maritim.case_cli import main

IMO = '1234567'
OS = '''<h1>Example</h1><table><tr><th>IMO Number</th><td>IMO1234567</td></tr>
<tr><th>MMSI</th><td>123456789</td></tr></table>
<table><tr><th colspan="6">Sanctions</th></tr>
<tr><th>Country</th><th>Authority</th><th>Program</th><th>Start date</th><th>End date</th><th></th></tr>
<tr><td>Australia</td><td>Example authority</td><td></td><td>2025-12-03</td><td>-</td><td>Details</td></tr></table>
<table><tr><th>Linked from</th></tr><tr><th>Subject</th><th>Role</th><th>Start date</th><th>End date</th><th></th></tr>
<tr><td>Example Ltd</td><td>commerce manager</td><td>2026-03-21</td><td>-</td><td>Details</td></tr></table>
<a href="/programs/UA-WS-MARE/">Program</a>
<a href="https://war-sanctions.gur.gov.ua/en/transport/ships/123">GUR</a>'''
GUR = '''<div><div>IMO</div><div>1234567</div></div>
<div><div>Vessel name</div><div>EXAMPLE</div></div>
<div><div>Flag (Current)</div><div>Example Flag</div></div>
<div><div>MMSI</div><div>123456789</div></div>
<div><div>Commercial ship manager (IMO / Country / Date)</div><div>Example Ltd (0000000 / Example / 21.03.2026)</div></div>
<p>On December 4, 2025, Australia imposed sanctions on the tanker.</p>'''
VF = '''<table><tr><td>IMO number</td><td>1234567</td></tr><tr><td>Vessel Name</td><td>EXAMPLE</td></tr>
<tr><td>Flag</td><td>Example Flag</td></tr></table><p>The vessel EXAMPLE (IMO 1234567, MMSI 123456789) is a tanker.</p>'''
UK = 'Report Date: 16-Nov-2026\nUnique ID,IMO number,Date Designated,Regime Name\nTEST1,IMO1234567,17/10/2024,Example regime\n'


def fake_fetch(url):
    if '/search/' in url:
        body = '<a href="/entities/EXAMPLE/">Example</a>'
    elif '/entities/' in url:
        body = OS
    elif '/ships/' in url:
        body = GUR
    elif '/vessels/' in url:
        body = VF
    elif url == vessel_lookup.UK_URL:
        body = UK
    else:
        raise ValueError('Unexpected test URL')
    return dict(url=url, body=body, retrieved='2026-11-16T00:00:00+00:00', epoch=0, cached=False)


class CollectorTests(unittest.TestCase):
    def test_full_collection_without_manual_case(self):
        result = vessel_lookup.collect(IMO, fake_fetch, lambda _: None)
        self.assertEqual(sum(s['status'] == 'collected' for s in result['sources'].values()), 4)
        self.assertTrue(any(r['value'] == 'TEST1' for r in result['records']))
        self.assertTrue(any(r['label'] == 'commerce manager' for r in result['records']))
        self.assertTrue(any('Australia has different' in w for w in result['warnings']))

    def test_wrong_imo_is_not_merged(self):
        for parser, body in [(vessel_lookup.parse_os, OS), (vessel_lookup.parse_gur, GUR), (vessel_lookup.parse_vf, VF)]:
            result = {'records': []}
            with self.assertRaises(ValueError):
                parser(body, '9299898', result, 'test')
            self.assertEqual(result['records'], [])

    def test_all_sources_unavailable_is_explicit(self):
        def fail(url):
            raise OSError('No network')
        result = vessel_lookup.collect(IMO, fail, lambda _: None)
        self.assertEqual(result['records'], [])
        self.assertTrue(all(s['status'] == 'unavailable' for s in result['sources'].values()))

    def test_visible_history_keeps_flag_and_date_without_guessing_name(self):
        body = VF + '''<div class="flx vhrow"><div><div class="vhl">Year</div><div class="vhv">2025 Jan</div></div>
        <div><div class="vhl">Flag</div><div class="vhv"><img title="Example Flag"></div></div>
        <div><div class="vhl">Vessel Name</div><div class="vhv"></div></div></div>'''
        result = {'records': []}
        vessel_lookup.parse_vf(body, IMO, result, 'vf')
        history = [r for r in result['records'] if r['section'] == 'History']
        self.assertEqual(history[0]['label'], 'VesselFinder change · 2025 Jan')
        self.assertEqual(history[0]['value'], 'Flag: Example Flag')

    def test_single_failed_source_keeps_other_findings(self):
        def partial(url):
            if 'vesselfinder' in url:
                raise OSError('HTTP 403')
            return fake_fetch(url)
        result = vessel_lookup.collect(IMO, partial, lambda _: None)
        self.assertTrue(result['records'])
        self.assertEqual(result['sources']['vf']['status'], 'unavailable')

    def test_uk_stream_filters_and_stops_after_exact_match(self):
        source = io.BytesIO(('Unique ID,IMO number,Date Designated\nWRONG,IMO91234567,01/01/2020\n' +
                             'RIGHT,IMO1234567,01/01/2021\nUNRELATED,IMO9999999,01/01/2022\n').encode())
        filtered = vessel_lookup.filter_uk_stream(source, IMO)
        self.assertIn('RIGHT', filtered)
        self.assertNotIn('WRONG', filtered)
        self.assertNotIn('UNRELATED', filtered)

    def test_uk_multiline_csv(self):
        source = io.BytesIO(b'Unique ID,IMO number,Other Information\nRIGHT,IMO1234567,"one\ntwo"\n')
        filtered = vessel_lookup.filter_uk_stream(source, IMO)
        self.assertEqual(list(csv.DictReader(io.StringIO(filtered)))[0]['Other Information'], 'one\ntwo')

    def test_url_allowlist_and_redirects(self):
        for url in ['https://127.0.0.1/', 'file:///secret', 'https://www.opensanctions.org.attacker.example/',
                    'https://user:password@www.opensanctions.org/', 'http://www.opensanctions.org/']:
            with self.assertRaises(ValueError):
                vessel_lookup.check_url(url)

    def test_html_report_escapes_source_content(self):
        result = vessel_lookup.collect(IMO, fake_fetch, lambda _: None)
        result['records'][0]['value'] = '<script>alert(1)</script>'
        with tempfile.TemporaryDirectory() as tmp:
            directory = vessel_lookup.export_scan(result, tmp)
            report = (directory / 'report.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>', report)
            self.assertIn('&lt;script&gt;', report)
            data = json.loads((directory / 'data.json').read_text(encoding='utf-8'))
            self.assertTrue(all('body' not in s for s in data['sources'].values()))
            self.assertTrue((directory / 'sources' / 'uk.txt').exists())

    def test_shorthand_cli_dispatch(self):
        with patch('Maritim.vessel_lookup.run_scan', return_value=0) as scan:
            self.assertEqual(main([IMO, '--no-open']), 0)
            scan.assert_called_once_with(IMO, None, False)

    def test_default_prompt_accepts_challenge_imo(self):
        with patch('builtins.input', return_value=''), patch('Maritim.vessel_lookup.run_scan', return_value=0) as scan:
            self.assertEqual(main([]), 0)
            scan.assert_called_once_with('9299898', None, True)


if __name__ == '__main__':
    unittest.main()
