import hashlib
from pathlib import Path
import unittest

from Maritim.historical_records import import_history, investigate


class QualifierTests(unittest.TestCase):
    def test_changed_photo_does_not_inherit_original_identification(self):
        photo = Path(__file__).resolve().parents[1] / 'Maritim' / 'web' / 'evidence' / 'lost-at-sea-1.png'
        original = photo.read_bytes()
        matched = investigate(hashlib.sha256(original).hexdigest(), assisted=False)
        self.assertEqual(matched['images'], [photo.name, 'starship-source.jpg'])
        changed = investigate(hashlib.sha256(original + b'edited').hexdigest(), assisted=True)
        self.assertEqual(changed['findings'], [])
        self.assertEqual(changed['images'], [])

    def test_inspection_uses_requested_detention_condition(self):
        rows = import_history(
            'kind,imo,date,condition,deficiency,source\n'
            'inspection,9305556,2026-01-14,Missing,Wrong item,https://example.org/other\n'
            'inspection,9305556,2026-01-14,NOT PROPERLY MAINTAINED,Test pump,https://example.org/inspection\n'
        )
        port, detention = investigate('IMO 9305556', assisted=False, imported=rows)['findings']
        self.assertIsNone(port['flag'])
        self.assertEqual(detention['flag'], 'flag{TEST_PUMP}')
        self.assertEqual(detention['status'], 'Investigator-supplied')
        self.assertIn('https://example.org/inspection', [s['url'] for s in detention['evidence']])
        self.assertNotIn('https://example.org/other', [s['url'] for s in detention['evidence']])

    def test_conflicting_voyages_stop_the_historical_identity_lookup(self):
        rows = import_history(
            'kind,container,vessel,imo,month,mmsi,source\n'
            'voyage,TEST1234567,Alpha,9243162,,,https://example.org/v1\n'
            'voyage,TEST1234567,Bravo,9305556,,,https://example.org/v2\n'
            'radio,,,9243162,2013-06,123456789,https://example.org/radio\n'
        )
        for records in (rows, list(reversed(rows))):
            with self.subTest(first=records[0][1]['url']):
                result, = investigate('TEST1234567', assisted=False, imported=records)['findings']
                self.assertEqual(result['status'], 'Conflicting records')
                self.assertIsNone(result['flag'])
                self.assertCountEqual([s['url'] for s in result['evidence']],
                                      ['https://example.org/v1', 'https://example.org/v2'])

    def test_historical_mmsi_requires_a_matching_container_voyage(self):
        rows = import_history(
            'kind,container,vessel,imo,month,mmsi,source\n'
            'voyage,TEST9999999,VELIKA,9243162,,,https://example.org/other-container\n'
            'radio,,,9243162,2013-06,367037568,https://example.org/radio\n'
        )
        result, = investigate('Check the status of temu1845369.', assisted=False, imported=rows)['findings']
        self.assertEqual(result['status'], 'Evidence needed')
        self.assertIsNone(result['flag'])
        self.assertEqual(result['evidence'], [])

    def test_undashed_port_date_is_rejected_with_format_hint(self):
        with self.assertRaisesRegex(ValueError, 'YYYY-MM-DD'):
            import_history('kind,imo,date,port,source\n'
                           'port,9305556,20260114,Sample port,https://example.org/port\n')


if __name__ == '__main__':
    unittest.main()
