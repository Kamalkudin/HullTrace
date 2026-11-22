"""Synthetic fixtures only: no asserted findings about a real vessel."""
import json
import tempfile
import unittest
from pathlib import Path

from Maritim.case_cli import evaluate, export_case, fresh_case, main, validate


def example_case():
    case = fresh_case('1234567', '2026-11-16')
    case['description'] = 'SYNTHETIC DEMO ONLY. Invented evidence, identifiers, and relationships; not an investigation result.'
    for sid in ('a', 'b'):
        case['sources'][sid] = {'title': 'Synthetic source ' + sid, 'publisher': 'Demo publisher ' + sid,
                                'url': 'https://example.org/' + sid, 'retrieved': '2026-11-16'}

    def add(kind, value, day, source='a', **fields):
        case['facts'].append(dict(id='f' + str(len(case['facts']) + 1), kind=kind, value=value, date=day,
                                  source=source, excerpt='Invented demonstration evidence only.', reviewed=True, **fields))

    add('mmsi', '123456789', '2026-11-16', scope='current')
    for flag in ('Comoros', 'Zimbabwe'):
        for source in ('a', 'b'):
            add('flag', flag, '2025-10-01', source, scope='historical', vessel_name='Range Demo', status='unverified')
    add('flag', 'Sierra Leone', '2026-11-16', scope='current')
    # A repeated historical current flag must also be excluded from the count.
    add('flag', 'Sierra Leone', '2026-03-01', scope='historical')
    for jurisdiction, day in [('United Kingdom', '2024-10-17'), ('European Union', '2024-12-16'),
                               ('Switzerland', '2024-12-23'), ('Canada', '2025-01-01'),
                               ('New Zealand', '2025-02-01'), ('Ukraine', '2025-03-01'), ('Australia', '2025-12-04')]:
        add('sanction', 'DEMO-' + jurisdiction.replace(' ', '-'), day, jurisdiction=jurisdiction,
            program='GB-RUS' if jurisdiction == 'United Kingdom' else 'DEMO', namespace='authority')
    for jurisdiction in ('European Union', 'Switzerland'):
        add('resolution', 'A.0000(00)', '2024-12-16', jurisdiction=jurisdiction)
    add('manager', 'Example Shipping Ltd', '2026-03-21', role='Commerce Manager', classification='Person of Interest')
    add('program', 'DEMO-MARE', '2026-01-01', registry='GUR maritime', namespace='agency')
    case['coverage'] = {topic: {'note': 'Synthetic fixture coverage only.'} for topic in ('sanctions', 'historical_flags')}
    return case


class InvestigatorTests(unittest.TestCase):
    def test_empty_case_cannot_answer(self):
        self.assertTrue(all(r['flag'] is None for r in evaluate(fresh_case('1234567', '2026-11-16'))))

    def test_six_challenge_formats(self):
        results = evaluate(example_case())
        self.assertEqual([r['flag'] for r in results], [
            'flag{123456789_Comoros_Zimbabwe}', 'flag{DEMO-UNITED-KINGDOM}',
            'flag{United_Kingdom_A.0000(00)}', 'flag{Example_Shipping_Ltd}',
            'flag{DEMO-MARE}', 'flag{Australia_2}'])

    def test_unreviewed_evidence_cannot_answer(self):
        case = example_case()
        for fact in case['facts']:
            fact['reviewed'] = False
        self.assertTrue(all(r['flag'] is None for r in evaluate(case)))

    def test_conflicting_current_mmsi(self):
        case = example_case()
        conflict = dict(case['facts'][0], id='conflict', value='987654321')
        case['facts'].append(conflict)
        self.assertIsNone(evaluate(case)[0]['flag'])

    def test_stale_snapshot(self):
        case = example_case()
        case['as_of'] = '2026-11-17'
        results = evaluate(case)
        self.assertIsNone(results[0]['flag'])
        self.assertIsNone(results[5]['flag'])

    def test_duplicate_and_case_variants_do_not_inflate_flags(self):
        case = example_case()
        case['facts'].append(dict(case['facts'][1], id='repeat', value='  COMOROS  '))
        self.assertEqual(evaluate(case)[5]['flag'], 'flag{Australia_2}')
        self.assertEqual(evaluate(case)[0]['flag'], 'flag{123456789_Comoros_Zimbabwe}')

    def test_same_publisher_is_not_corroboration(self):
        case = example_case()
        case['sources']['b']['publisher'] = ' Demo PUBLISHER a '
        self.assertIsNone(evaluate(case)[0]['flag'])

    def test_aggregator_program_is_not_agency_identifier(self):
        case = example_case()
        case['facts'][-1]['namespace'] = 'aggregator'
        self.assertIsNone(evaluate(case)[4]['flag'])

    def test_missing_coverage_blocks_chronology_and_count(self):
        case = example_case()
        case['coverage'] = {}
        results = evaluate(case)
        self.assertIsNone(results[2]['flag'])
        self.assertIsNone(results[5]['flag'])

    def test_jurisdiction_outside_window(self):
        case = example_case()
        for fact in case['facts']:
            if fact.get('jurisdiction') == 'Australia':
                fact['date'] = '2026-01-01'
        self.assertIsNone(evaluate(case)[5]['flag'])

    def test_earliest_date_tie_is_unresolved(self):
        case = example_case()
        for fact in case['facts']:
            if fact['kind'] == 'sanction' and fact.get('jurisdiction') == 'Canada':
                fact['date'] = '2024-10-17'
        self.assertIsNone(evaluate(case)[2]['flag'])

    def test_first_country_does_not_treat_eu_as_country(self):
        case = example_case()
        for fact in case['facts']:
            if fact['kind'] == 'sanction' and fact['jurisdiction'] == 'European Union':
                fact['date'] = '2024-01-01'
        self.assertEqual(evaluate(case)[2]['flag'], 'flag{United_Kingdom_A.0000(00)}')

    def test_resolution_requires_both_december_records(self):
        case = example_case()
        for fact in case['facts']:
            if fact['kind'] == 'resolution' and fact['jurisdiction'] == 'Switzerland':
                fact['date'] = '2025-01-01'
        self.assertIsNone(evaluate(case)[2]['flag'])

    def test_bad_imo_and_missing_source(self):
        with self.assertRaises(ValueError):
            fresh_case('1234560', '2026-11-16')
        case = example_case()
        case['facts'][0]['source'] = 'absent'
        with self.assertRaises(ValueError):
            validate(case)

    def test_cli_and_export_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = Path(tmp) / 'case.json'
            self.assertEqual(main(['init', str(case_path), '--imo', '1234567', '--as-of', '2026-11-16']), 0)
            self.assertEqual(main(['source', str(case_path), '--id', 's1', '--url', 'https://example.org',
                                   '--title', 'Synthetic', '--publisher', 'Demo', '--retrieved', '2026-11-16']), 0)
            self.assertEqual(main(['fact', str(case_path), '--id', 'f1', '--kind', 'mmsi', '--value', '123456789',
                                   '--source', 's1', '--date', '2026-11-16', '--excerpt', 'Synthetic only', '--scope', 'current', '--reviewed']), 0)
            case = json.loads(case_path.read_text(encoding='utf-8'))
            self.assertTrue(case['facts'][0]['reviewed'])
            export_case(example_case(), Path(tmp) / 'export')
            report = (Path(tmp) / 'export' / 'report.md').read_text(encoding='utf-8')
            self.assertIn('https://example.org/a', report)
            self.assertIn('Example Shipping Ltd', report)
            self.assertTrue((Path(tmp) / 'export' / 'graph.dot').exists())


if __name__ == '__main__':
    unittest.main()
