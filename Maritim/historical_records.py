"""Resolve the Qualifier A maritime clues against a small, attributed evidence pack."""
import csv
from datetime import date
from io import StringIO
import re
from urllib.parse import urlsplit

from Maritim.case_cli import valid_imo

CSV_MAX_BYTES = 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), CSV_MAX_BYTES))


WALK = 'https://www.maltego.com/blog/maltego-ctf-championship-qualifier-a-challenge-walkthrough/'
CNN = 'https://edition.cnn.com/2026/08/03/science/spacex-starship-indian-ocean-recovery-satellite-images'
GFW = 'https://globalfishingwatch.org/platform/vessel/e4248b7f5-57cb-a0df-46f4-f66792d78684'
VF = 'https://www.vesselfinder.com/vessels/details/'
UPLOADS = 'https://www.maltego.com/images/uploads/'

PHOTOS = {
    'lost-at-sea-1.png': 'c9bedfc5c0d497a0972434a706dc54f0dfb3c9570fddb7164eb5f6cc1859dd45',
    'lost-at-sea-2.png': '06e4459069877221b5ff90619eac9cd99ce0259ec9764be239c6ecb1936812bf',
    'the-renamed-vessel.png': 'b851e5c1ec1f82409a28faab26228f68d491567b6ec30c1a17d33574ad449985',
}


def record(label, url, detail, **fields):
    return dict(source=label, url=url, detail=detail, fields=fields)


OBJECT = record('CNN recovery report', CNN, 'The recovery report identifies the spacecraft in the matched photograph.', object='Ship 40')
TUG = record('CNN recovery photograph', CNN, 'The caption names the tug alongside the spacecraft.', vessel='Normand Ranger')
RANGER = record('VesselFinder', VF + '9413432', 'The registry entry links the tug name to its permanent identifier.', vessel='Normand Ranger', imo='9413432')
MARVEN = record('Global Fishing Watch', GFW, 'This April–August 2026 identity record connects the callsign to the hull used to search earlier events.', callsign='TJM7W3', vessel='MARVEN', imo='9305556')
PORT = record('Maltego walkthrough', WALK, 'The organizer reports this historic port visit from the vessel track.', imo='9305556', date='2026-01-14', port='Yarimca')
PSC = record('Maltego walkthrough', WALK, 'The organizer transcribes this ground for detention from the Equasis inspection.', imo='9305556', date='2026-01-14', condition='Not properly maintained', deficiency='Emergency fire pump')
VOYAGE = record('Maltego walkthrough', WALK, 'The organizer links the container to this vessel through a voyage lookup.', container='TEMU1845369', vessel='VELIKA', imo='9243162')
VELIKA = record('VesselFinder', VF + '9243162', 'The vessel record corroborates the name and IMO.', vessel='VELIKA', imo='9243162')
RADIO = record('Maltego walkthrough', WALK, 'The organizer attributes this MMSI to the June 2013 identity period.', imo='9243162', month='2013-06', mmsi='367037568')

HISTORY = {'port': PORT, 'inspection': PSC, 'voyage': VOYAGE, 'radio': RADIO}


def import_history(content):
    if not isinstance(content, str):
        raise ValueError('Evidence must be CSV text.')
    imported = []
    for row in csv.DictReader(StringIO(content.lstrip('\ufeff')), strict=True):
        kind = (row.get('kind') or '').strip().lower()
        url = (row.get('source') or '').strip()
        if kind not in HISTORY or urlsplit(url).scheme not in {'https', 'http'} or not urlsplit(url).hostname:
            raise ValueError('Each CSV row needs a supported kind and a public source URL.')
        fields = {key: (row.get(key) or '').strip() for key in HISTORY[kind]['fields']}
        if not all(fields.values()):
            raise ValueError('Missing fields for ' + kind + ': ' + ', '.join(fields))
        valid_imo(fields['imo'])
        if 'date' in fields or 'month' in fields:
            monthly = 'month' in fields
            observation_date = fields['month'] + '-01' if monthly else fields['date']
            expected_format = 'YYYY-MM' if monthly else 'YYYY-MM-DD'
            try:
                canonical = date.fromisoformat(observation_date).isoformat()
            except ValueError:
                canonical = None
            if observation_date != canonical:
                raise ValueError(f'Enter a valid calendar date in {expected_format} format.')
        if kind == 'radio' and not re.fullmatch(r'\d{9}', fields['mmsi']):
            raise ValueError('Historical MMSI must contain nine digits.')
        imported.append((kind, record('Investigator record', url, 'Submitted with this investigation.', **fields)))
    if content.strip() and not imported:
        raise ValueError('The CSV must include a header and at least one evidence row.')
    return imported


def select_history(kind, imported, assisted, **criteria):
    records = [row for tag, row in imported if tag == kind]
    if assisted:
        records.append(HISTORY[kind])
    return [row for row in records if all(row['fields'].get(k, '').casefold() == v.casefold()
                                         for k, v in criteria.items())]

def finding(title, steps, format_string, missing=''):
    evidence = []
    values = {}
    for step in steps:
        matches = step if isinstance(step, list) else [step]
        if not matches:
            return dict(title=title, status='Evidence needed', evidence=evidence, flag=None, note=missing)
        evidence.extend(matches)
        if any({k: v.casefold() for k, v in row['fields'].items()} !=
               {k: v.casefold() for k, v in matches[0]['fields'].items()} for row in matches[1:]):
            return dict(title=title, status='Conflicting records', evidence=evidence, flag=None,
                        note='The matching records disagree. Compare their sources before submitting a flag.')
        values.update(matches[0]['fields'])
    answer = re.sub(r'\s+', '_', format_string.format(**values).strip().upper())
    status = 'Source-backed'
    if any(s['source'] == 'Investigator record' for s in evidence):
        status = 'Investigator-supplied'
    if any(s['url'] == WALK for s in evidence):
        status = 'Walkthrough-assisted'
    return dict(title=title, status=status, evidence=evidence, flag='flag{' + answer + '}', note='')


def investigate(clue, assisted, imported=()):
    if not isinstance(clue, str) or not isinstance(assisted, bool):
        raise ValueError('Supply a text clue and a walkthrough setting.')
    key = clue.strip().upper()
    if not key:
        raise ValueError('Enter a callsign or container number, or select an image.')
    photo = next((name for name, digest in PHOTOS.items() if digest.upper() == key), None)
    steps = []
    images = []
    if photo:
        images.append(photo)
        if photo.startswith('lost-at-sea'):
            images.append('starship-source.jpg')
            steps.append(record('Challenge photograph', UPLOADS + photo, 'The supplied image matches the archived challenge file. Compare the tug, rocket orientation and small boat with the Vantor photograph published by CNN.'))
            if photo == 'lost-at-sea-1.png':
                result = finding('Lost at Sea · 1', steps + [OBJECT], '{object}')
            else:
                result = finding('Lost at Sea · 2', steps + [TUG, RANGER], '{vessel}_{imo}')
            findings = [result]
            return dict(label='Offshore recovery photograph', images=images, findings=findings)
        steps.append(record('Pager screenshot', UPLOADS + photo, 'The decoded text on the receiver repeats the container identifier.', container='TEMU1845369'))
        key = steps[-1]['fields']['container']
    # Accept the decoded pager sentence as well as a copied identifier.
    tokens = set(re.findall(r'[A-Z0-9]+', key))
    if MARVEN['fields']['callsign'] in tokens or MARVEN['fields']['imo'] in tokens:
        event = dict(imo=MARVEN['fields']['imo'], date='2026-01-14')
        findings = [
            finding('Flags, Ports & Detentions · 1', [MARVEN, select_history('port', imported, assisted, **event)], 'PORT_{port}',
                    'A port-call or track record for IMO 9305556 on 14 January 2026 is needed.'),
            finding('Flags, Ports & Detentions · 2', [MARVEN, select_history('inspection', imported, assisted, **event, condition='Not properly maintained')], '{deficiency}',
                    'The dated inspection entry with its ground for detention is needed.'),
        ]
        return dict(label='Radio callsign · TJM7W3', images=images, findings=findings)
    container = next((token for token in sorted(tokens) if re.fullmatch(r'[A-Z]{4}\d{7}', token)), None)
    if container:
        voyages = select_history('voyage', imported, assisted, container=container)
        if not voyages and container != VOYAGE['fields']['container']:
            return dict(label='No archived voyage for ' + container, images=images, findings=[])
        steps.append(voyages)
        if voyages:
            imo = voyages[0]['fields']['imo']
            if imo == VELIKA['fields']['imo'] and voyages[0]['fields']['vessel'].casefold() == VELIKA['fields']['vessel'].casefold():
                steps.append(VELIKA)
            steps.append(select_history('radio', imported, assisted, imo=imo, month='2013-06'))
        result = finding('The Renamed Vessel', steps, '{mmsi}',
                         'The historical container voyage and a June 2013 MMSI record are needed. A current vessel profile cannot establish either link.')
        return dict(label='Shipping container · ' + container, images=images, findings=[result])
    return dict(label='No matching clue in the Qualifier A maritime evidence pack', images=[], findings=[])
