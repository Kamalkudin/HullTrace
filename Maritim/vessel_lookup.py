"""Bounded public-source collection and a self-contained vessel report."""
import concurrent.futures
import csv
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urljoin, urlparse
import urllib.request
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent
UK_URL = 'https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv'
ALLOWED = {'www.opensanctions.org', 'war-sanctions.gur.gov.ua', 'www.vesselfinder.com', 'sanctionslist.fcdo.gov.uk'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def clean(value):
    return re.sub(r'\s+', ' ', value).strip()


class Node:
    def __init__(self, tag='', attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or []), []

    def text(self):
        if self.tag in {'script', 'style', 'svg'}:
            return ''
        return clean(' '.join(child.text() if isinstance(child, Node) else child for child in self.children))

    def find(self, tag):
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    yield child
                yield from child.find(tag)


class Document(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def links(root, base):
    result = []
    for a in root.find('a'):
        url = urljoin(base, a.attrs.get('href', ''))
        if urlparse(url).scheme in {'https', 'http'} and url not in [x['url'] for x in result]:
            result.append({'label': a.text(), 'url': url})
    return result


def cells(row):
    return [n for n in row.children if isinstance(n, Node) and n.tag in {'td', 'th'}]


def properties(root):
    result = {}
    for row in root.find('tr'):
        values = cells(row)
        if len(values) >= 2 and values[0].text():
            result.setdefault(values[0].text(), values[1].text())
    return result


def check_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname not in ALLOWED or p.port not in {None, 443} or p.username or p.password:
        raise ValueError('Unsupported public-source URL')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, cache_dir=None, imo=None):
    """Six-hour cache; a failed request is reported, never replaced with stale data."""
    check_url(url)
    cache_dir = Path(cache_dir or ROOT / '.cache')
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = url + (':' + imo if url == UK_URL and imo else '')
    cache = cache_dir / (hashlib.sha256(cache_key.encode()).hexdigest() + '.json')
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding='utf-8'))
            if data['url'] == url and 0 <= time.time() - data['epoch'] < 21600:
                return dict(data, cached=True)
        except (ValueError, KeyError, OSError):
            pass
    limit = 40_000_000 if url == UK_URL else 4_000_000
    request = urllib.request.Request(url, headers={'User-Agent': 'MaritimeInvestigator/0.2 (public-source vessel research)'})
    opener = urllib.request.build_opener(SafeRedirect())
    with opener.open(request, timeout=25) as response:
        if url == UK_URL and imo:
            body = filter_uk_stream(response, imo)
        else:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ValueError('Source exceeded download size limit')
            body = raw.decode(response.headers.get_content_charset() or 'utf-8-sig', errors='replace')
        data = {'url': url, 'body': body,
                'retrieved': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'epoch': time.time(), 'cached': False}
    temp = cache.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(cache)
    return data


def filter_uk_stream(response, imo):
    """Keep the first exact IMO match; do not download/archive unrelated personal records."""
    started = time.monotonic()
    def lines():
        size = 0
        for line in response:
            size += len(line)
            if size > 120_000_000 or time.monotonic() - started > 55:
                raise ValueError('UK list scan limit reached; no complete negative result is available')
            yield line.decode('utf-8-sig', errors='replace')
    header = None
    output = io.StringIO()
    writer = csv.writer(output)
    for row in csv.reader(lines()):
        if header is None:
            if 'Unique ID' in row:
                header = row
                writer.writerow(header)
            continue
        record = dict(zip(header, row))
        if any(re.search(r'(?<!\d)' + imo + r'(?!\d)', value) for key, value in record.items() if 'IMO' in key.upper()):
            writer.writerow(row)
            return output.getvalue()
    if header is None:
        raise ValueError('UK list header was not found')
    return output.getvalue()


def add(result, section, label, value, source, **extra):
    value = clean(str(value))
    if value and value != '-' and 'more…' not in value:
        record = dict(section=section, label=label, value=value, source=source, **extra)
        if record not in result['records']:
            result['records'].append(record)


def parse_os(body, imo, result, sid):
    root = Document(body).root
    props = properties(root)
    if not re.search(r'(?<!\d)' + imo + r'(?!\d)', props.get('IMO Number', '')):
        raise ValueError('Profile does not contain a matching IMO field')
    mapping = {'Name': ('Identity', 'Names in source records'), 'Previous name': ('History', 'Previous names'),
               'MMSI': ('Identity', 'MMSI'), 'Call Sign': ('Identity', 'Call sign'),
               'Flag': ('History', 'Flags in source records (dates vary)'), 'Past Flags': ('History', 'Former flags'),
               'Build Date': ('Specifications', 'Build year'), 'Type': ('Specifications', 'Vessel type'),
               'Deadweight Tonnage': ('Specifications', 'Deadweight (t)'), 'Gross Registered Tonnage': ('Specifications', 'Gross tonnage'),
               'Topics': ('Sanctions', 'OpenSanctions classifications'), 'Last change': ('Identity', 'Source last change')}
    for key, (section, label) in mapping.items():
        add(result, section, label, props.get(key, ''), sid)
    # Tables contain source-reported sanctions dates and organization relationships.
    for table in root.find('table'):
        rows = list(table.find('tr'))
        if not rows:
            continue
        if len(cells(rows[0])) == 1:
            rows = rows[1:]
        if not rows:
            continue
        headers = [c.text() for c in cells(rows[0])]
        if 'Country' in headers and 'Authority' in headers:
            for row in rows[1:]:
                values = [c.text() for c in cells(row)]
                if len(values) < len(headers):
                    continue
                record = dict(zip(headers, values))
                country, day = record.get('Country', ''), record.get('Start date', '-')
                if country:
                    add(result, 'Sanctions', country, day + ' | ' + record.get('Authority', ''), sid,
                        jurisdiction=country, date=day if re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) else None,
                        note='Source-reported start date; a registry entry is not necessarily a legal designation.')
        if ('Subject' in headers or 'Owner' in headers) and 'Role' in headers:
            for row in rows[1:]:
                values = [c.text() for c in cells(row)]
                if len(values) < len(headers):
                    continue
                record = dict(zip(headers, values))
                add(result, 'Companies', record.get('Role') or 'Linked entity',
                    (record.get('Subject') or record.get('Owner', '')) + ' | start ' + record.get('Start date', '-'), sid)
    for link in links(root, 'https://www.opensanctions.org'):
        match = re.fullmatch(r'/programs/([^/]+)/', urlparse(link['url']).path)
        if match:
            add(result, 'Programs', 'OpenSanctions program code', match[1], sid,
                note='Aggregator taxonomy; not proof of an agency-issued identifier.')
    return links(root, 'https://www.opensanctions.org')


def parse_gur(body, imo, result, sid):
    root = Document(body).root
    # Labels live in sibling divs. Reading leaf labels avoids navigation and crew details.
    mapping = {'Vessel name': ('Identity', 'Reported vessel name'), 'Flag (Current)': ('Identity', 'Current flag'),
               'MMSI': ('Identity', 'MMSI'), 'Call sign': ('Identity', 'Call sign'), 'Vessel Type': ('Specifications', 'Vessel type'),
               'Length (m)': ('Specifications', 'Length (m)'), 'Gross tonnage (t)': ('Specifications', 'Gross tonnage'),
               'DWT (t)': ('Specifications', 'Deadweight (t)'), 'Build year': ('Specifications', 'Build year'),
               'Builder (country)': ('Specifications', 'Builder'), 'Former ship names': ('History', 'Previous names'),
               'Flags (former)': ('History', 'Former flags'), 'Ship Owner (IMO / Country / Date)': ('Companies', 'Registered owner'),
               'Commercial ship manager (IMO / Country / Date)': ('Companies', 'Commercial manager'),
               'Ship Safety Management Manager (IMO / Country / Date)': ('Companies', 'Safety manager'),
               'Cases of AIS shutdown': ('Activity', 'AIS shutdown reported by GUR'),
               'Calling at russian ports': ('Activity', 'Russian port calls reported by GUR'),
               'Visited ports': ('Activity', 'Ports reported by GUR'), 'Sanctions': ('Sanctions', 'Jurisdictions reported by GUR')}
    found = {}
    for parent in root.find('div'):
        children = [n for n in parent.children if isinstance(n, Node)]
        for index, child in enumerate(children[:-1]):
            label = child.text()
            if label in mapping or label == 'IMO':
                found.setdefault(label, children[index + 1].text())
    if found.get('IMO') != imo:
        raise ValueError('GUR profile does not contain a matching IMO field')
    for key, (section, label) in mapping.items():
        add(result, section, label, found.get(key, ''), sid)
    text = root.text()
    updated = re.search(r'Updated:\s*(\d{2}\.\d{2}\.\d{4})', text)
    if updated:
        add(result, 'Identity', 'GUR profile updated', updated[1], sid)
    for code in sorted(set(re.findall(r'A\.\d+\(\d+\)', text))):
        add(result, 'Programs', 'IMO resolution cited by GUR', code, sid)
    # Only parse single-date vessel-designation sentences; do not infer dates from generic metadata.
    for sentence in re.findall(r'(?:On|Since) [^.]{10,500}\.', text):
        if not re.search(r'sanctions on the (?:vessel|tanker)', sentence, re.I):
            continue
        match = re.search(r'(\d{1,2} [A-Z][a-z]+ \d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})', sentence)
        if not match:
            continue
        parsed = None
        for fmt in ('%d %B %Y', '%B %d, %Y'):
            try:
                parsed = dt.datetime.strptime(match[1], fmt).date().isoformat()
                break
            except ValueError:
                pass
        country = next((c for c in ['United Kingdom', 'Canada', 'New Zealand', 'Australia', 'Ukraine'] if c in sentence), None)
        if parsed and country:
            add(result, 'Sanctions', country, parsed + ' | GUR narrative', sid, jurisdiction=country, date=parsed)
    pair = re.search(r'Since ([A-Z][a-z]+) (\d{1,2}) and (\d{1,2}), (\d{4}), the EU and Switzerland have imposed sanctions on the tanker', text)
    if pair:
        for country, day in [('European Union', pair[2]), ('Switzerland', pair[3])]:
            parsed = dt.datetime.strptime(f'{pair[1]} {day} {pair[4]}', '%B %d %Y').date().isoformat()
            add(result, 'Sanctions', country, parsed + ' | GUR narrative', sid, jurisdiction=country, date=parsed)
    return links(root, 'https://war-sanctions.gur.gov.ua')


def parse_vf(body, imo, result, sid):
    root = Document(body).root
    props = properties(root)
    if props.get('IMO number') != imo:
        raise ValueError('VesselFinder profile unavailable or IMO field does not match')
    mapping = {'Vessel Name': ('Identity', 'Reported vessel name'), 'Flag': ('Identity', 'Current flag'),
               'Ship Type': ('Specifications', 'Vessel type'), 'Year of Build': ('Specifications', 'Build year'),
               'Gross Tonnage': ('Specifications', 'Gross tonnage'), 'Deadweight (t)': ('Specifications', 'Deadweight (t)'),
               'Length Overall (m)': ('Specifications', 'Length (m)'), 'Beam (m)': ('Specifications', 'Beam (m)'),
               'Callsign': ('Identity', 'Call sign'), 'Navigation Status': ('Activity', 'Navigation status'),
               'Position Received': ('Activity', 'AIS age reported at retrieval'), 'Destination': ('Activity', 'Reported destination'),
               'Current draught': ('Activity', 'Reported draught')}
    for key, (section, label) in mapping.items():
        add(result, section, label, props.get(key, ''), sid)
    text = root.text()
    match = re.search(r'IMO\s*' + imo + r',\s*MMSI\s*(\d{9})', text)
    if match:
        add(result, 'Identity', 'MMSI', match[1], sid)
    # Small publicly visible snapshot; this is not a live AIS feed.
    match = re.search(r'The current position of .{1,900}?(?=The vessel [A-Z]|Sanctions & Bans|Track on Map)', text)
    if match:
        add(result, 'Activity', 'VesselFinder voyage snapshot', match[0], sid)
    for row in root.find('div'):
        if 'vhrow' not in row.attrs.get('class', '').split():
            continue
        fields = {}
        for cell in row.children:
            if not isinstance(cell, Node):
                continue
            labels = [n.text() for n in cell.find('div') if 'vhl' in n.attrs.get('class', '').split()]
            vals = [n for n in cell.find('div') if 'vhv' in n.attrs.get('class', '').split()]
            if labels and vals:
                value = vals[0].text()
                if labels[0] == 'Flag':
                    value = ' / '.join(n.attrs.get('title', '') for n in vals[0].find('img'))
                if value:
                    fields[labels[0]] = value
        period = fields.pop('Year', '')
        if period and fields:
            add(result, 'History', 'VesselFinder change · ' + period,
                '; '.join(k + ': ' + v for k, v in fields.items()), sid,
                note='Month-level event as published; blank/private fields are omitted. Name-to-flag intervals are not inferred.')


def parse_uk(body, imo, result, sid):
    rows = csv.reader(io.StringIO(body.lstrip('\ufeff')))
    header = None
    matches = []
    for row in rows:
        if header is None:
            if any('Unique ID' == v.strip() for v in row):
                header = [v.strip() for v in row]
            continue
        data = dict(zip(header, row))
        ship_fields = [v for k, v in data.items() if 'IMO' in k.upper()]
        if any(re.search(r'(?<!\d)' + imo + r'(?!\d)', v) for v in ship_fields):
            matches.append(data)
    if header is None:
        raise ValueError('UK list column format changed; no records interpreted')
    for data in matches:
        for key, value in data.items():
            if value and key in {'Unique ID', 'Regime Name', 'Date Designated', 'Last Updated', 'Sanctions Imposed', 'Name 6', 'Current owner/operator (s)', 'Previous owner/operator (s)', 'Current believed flag of ship', 'Previous flags'}:
                add(result, 'UK official record', 'Listed vessel name' if key == 'Name 6' else key, value, sid,
                    **({'note': 'Value in the sanctions record; may reflect the designation-era identity.'} if 'flag' in key else {}))
        try:
            designated = dt.datetime.strptime(data.get('Date Designated', ''), '%d/%m/%Y').date().isoformat()
            add(result, 'Sanctions', 'United Kingdom', designated + ' | official UK list', sid, jurisdiction='United Kingdom', date=designated)
        except ValueError:
            pass
    return bool(matches)


def source_job(sid, label, url, fetcher):
    try:
        return sid, dict(fetcher(url), label=label, status='downloaded')
    except Exception as error:
        return sid, {'url': url, 'label': label, 'status': 'unavailable', 'error': str(error)}


def collect(imo, fetcher=None, progress=print):
    from Maritim.case_cli import valid_imo
    valid_imo(imo)
    fetcher = fetcher or (lambda url: fetch(url, imo=imo))
    result = {'imo': imo, 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
              'sources': {}, 'records': [], 'links': [], 'warnings': []}
    jobs = [('search', 'OpenSanctions search', 'https://www.opensanctions.org/search/?' + urlencode({'q': imo})),
            ('vf', 'VesselFinder', 'https://www.vesselfinder.com/vessels/details/' + imo),
            ('uk', 'UK FCDO sanctions list', UK_URL)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(source_job, *job, fetcher) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            sid, data = future.result()
            result['sources'][sid] = data
            progress(data['label'] + ': ' + data['status'])

    def parse(sid, parser):
        data = result['sources'][sid]
        if 'body' not in data:
            return []
        # Parse into a temporary record list so failed parsers cannot leave partial findings.
        parsed = {'records': []}
        try:
            extras = parser(data['body'], imo, parsed, sid)
            result['records'].extend(parsed['records'])
            data['status'] = 'collected' if parsed['records'] else 'no matching public record'
            return extras or []
        except (ValueError, KeyError, IndexError, TypeError) as error:
            data['status'], data['error'] = 'could not interpret', str(error)
            return []

    parse('vf', parse_vf)
    parse('uk', parse_uk)
    search = result['sources']['search']
    if 'body' in search:
        candidates = [x['url'] for x in links(Document(search['body']).root, search['url'])
                      if urlparse(x['url']).hostname == 'www.opensanctions.org' and re.fullmatch('/entities/[^/]+/', urlparse(x['url']).path)]
        search['status'] = 'discovery only'
        for index, url in enumerate(candidates[:3]):
            sid = 'os' + str(index + 1)
            _, data = source_job(sid, 'OpenSanctions vessel profile', url, fetcher)
            result['sources'][sid] = data
            discovered = parse(sid, parse_os)
            progress(data['label'] + ': ' + data['status'])
            if data['status'] == 'collected':
                gur_urls = [x['url'] for x in discovered if urlparse(x['url']).hostname == 'war-sanctions.gur.gov.ua'
                            and re.fullmatch(r'/en/transport/ships/\d+', urlparse(x['url']).path)]
                result['links'].extend(x for x in discovered if urlparse(x['url']).hostname != 'www.opensanctions.org')
                if gur_urls:
                    _, gur = source_job('gur', 'GUR War & Sanctions', gur_urls[0], fetcher)
                    result['sources']['gur'] = gur
                    result['links'].extend(parse('gur', parse_gur))
                    progress('GUR War & Sanctions: ' + gur['status'])
                break
        if not candidates:
            result['warnings'].append('No OpenSanctions profile discovered. This is not a clean sanctions result.')
    if 'gur' not in result['sources']:
        result['warnings'].append('GUR profile could not be discovered from the available source links.')
    for label in ('MMSI', 'Current flag', 'Reported vessel name'):
        vals = {r['value'].casefold() for r in result['records'] if r['label'] == label}
        if len(vals) > 1:
            result['warnings'].append('Sources disagree on ' + label + '; see the source-labelled records.')
    dates = {}
    for record in result['records']:
        if record.get('date'):
            dates.setdefault(record['jurisdiction'], set()).add(record['date'])
    for country, values in dates.items():
        if len(values) > 1:
            result['warnings'].append(country + ' has different reported sanctions dates: ' + ', '.join(sorted(values)) + '. Check the legal notice.')
    result['warnings'].append('Coverage is limited to accessible public pages. Missing results do not establish an absence of sanctions or history.')
    return result


def challenge_leads(result):
    records = result['records']
    values = lambda label: sorted({r['value'] for r in records if r['label'] == label})
    mmsis = values('MMSI')
    ukids = values('Unique ID')
    companies = [r['value'] for r in records if r['section'] == 'Companies' and any(x in r['label'].lower() for x in ('commercial', 'commerce'))]
    codes = values('OpenSanctions program code')
    resolutions = values('IMO resolution cited by GUR')
    dates = [r for r in records if r.get('date') and r['jurisdiction'] not in {'European Union', 'United Nations'}]
    first = min(dates, key=lambda r: r['date']) if dates else None
    return [
        ('1 · Identity and historical flags', 'MMSI: ' + (', '.join(mmsis) or 'not found') + '. Flag/name histories are below; name-specific flag periods still need corroboration.'),
        ('2 · UK identifier', 'Official UK identifier: ' + (', '.join(ukids) or 'not found') + ('. Candidate: flag{' + ukids[0].upper() + '}' if len(ukids) == 1 else '')),
        ('3 · Earliest sanctions and resolution', ('Earliest collected dated national record: ' + first['jurisdiction'] + ' (' + first['date'] + '). ' if first else 'No dated national sanctions record found. ') + 'Cited resolution: ' + (', '.join(resolutions) or 'not found') + '. Chronology is not exhaustive; compare official EU and Swiss records.'),
        ('4 · Commercial manager', '; '.join(companies) if companies else 'No commercial manager found in accessible records.'),
        ('5 · Program identifier', 'OpenSanctions codes: ' + (', '.join(codes) or 'not found') + '. These are aggregator codes; agency issuance is not established.'),
        ('6 · Additional jurisdiction and flag count', 'Compare the jurisdictions and former flags below. Counts are not certified complete; the current flag and false/unverified annotations need reconciliation.')]


def export_scan(result, out=None):
    directory = Path(out).resolve() if out else ROOT / 'output' / 'scans' / result['imo'] / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True, exist_ok=True)
    raw_dir = directory / 'sources'
    raw_dir.mkdir(exist_ok=True)
    public = dict(result, sources={})
    for sid, source in result['sources'].items():
        public['sources'][sid] = {k: v for k, v in source.items() if k != 'body'}
        if 'body' in source:
            # Save HTML as text to prevent scripts executing when an archive is opened.
            (raw_dir / (sid + '.txt')).write_text(source['body'], encoding='utf-8')
            public['sources'][sid]['snapshot'] = 'sources/' + sid + '.txt'
    (directory / 'data.json').write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding='utf-8')
    esc = lambda s: html.escape(str(s), quote=True)
    report = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
              '<title>Vessel report · IMO ' + result['imo'] + '</title>',
              '<style>body{font:16px/1.6 Segoe UI,Arial,sans-serif;background:#f2f5f8;color:#172b40;margin:0}main{max-width:1100px;margin:auto;padding:36px 22px}h1{font-size:36px;margin:4px 0}h2{margin-top:32px}h3{margin-top:0}a{color:#145fa6}table{width:100%;border-collapse:collapse;background:white}th,td{padding:12px;text-align:left;border-bottom:1px solid #dce3eb;vertical-align:top;overflow-wrap:anywhere}th{background:#e7eef6}td:first-child{width:25%}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.card{background:white;border:1px solid #dce3eb;border-radius:12px;padding:20px}.note{background:#fff2d1;border-left:4px solid #cc8b00;padding:12px 18px}.muted{color:#536779;font-size:14px}.pill{font-size:13px;letter-spacing:2px;color:#236286}@media(max-width:600px){h1{font-size:28px}th,td{padding:7px;font-size:13px}}@media print{body{background:white}.card{break-inside:avoid}}</style><main>',
              '<div class="pill">MARITIME INVESTIGATOR</div><h1>IMO ' + result['imo'] + '</h1>',
              '<p class="muted">Collected ' + esc(result['generated_at']) + ' · timestamps are UTC · public-source findings, not independently verified conclusions</p>']
    md = ['# Vessel report · IMO ' + result['imo'], '', 'Collected ' + result['generated_at'] + ' (UTC).', '', 'Public-source findings; not independently verified conclusions.', '']
    headline = []
    for label in ('Reported vessel name', 'MMSI', 'Current flag', 'Build year'):
        vals = sorted({r['value'] for r in result['records'] if r['label'] == label})
        headline.append('<div class="card"><div class="muted">' + label + '</div><strong>' + esc(' / '.join(vals) or 'Not available') + '</strong></div>')
    report.append('<div class="cards">' + ''.join(headline) + '</div>')
    report.append('<h2>Read before drawing conclusions</h2><div class="note"><ul>' + ''.join('<li>' + esc(w) + '</li>' for w in result['warnings']) + '</ul></div>')
    md.extend('- ' + w for w in result['warnings'])
    report.append('<h2>Your six challenge leads</h2><div class="cards">')
    for title, body in challenge_leads(result):
        report.append('<div class="card"><h3>' + esc(title) + '</h3><p>' + esc(body) + '</p></div>')
        md += ['', '## ' + title, '', body]
    report.append('</div>')
    for section in ('Identity', 'Specifications', 'History', 'Companies', 'Sanctions', 'UK official record', 'Programs', 'Activity'):
        records = [r for r in result['records'] if r['section'] == section]
        report.append('<h2>' + section + '</h2>')
        md += ['', '## ' + section, '']
        if not records:
            report.append('<p>No information collected for this section.</p>')
            md.append('No information collected for this section.')
            continue
        report.append('<table><tr><th>Field</th><th>Finding</th><th>Source</th></tr>')
        for record in records:
            source = result['sources'][record['source']]
            report.append('<tr><td>' + esc(record['label']) + '</td><td>' + esc(record['value']) + ('<div class="muted">' + esc(record['note']) + '</div>' if record.get('note') else '') + '</td><td><a href="' + esc(source['url']) + '">' + esc(source['label']) + '</a></td></tr>')
            md.append('- **' + record['label'] + '**: ' + record['value'] + ' — [' + source['label'] + '](' + source['url'] + ')')
        report.append('</table>')
    report.append('<h2>Source coverage</h2><table><tr><th>Source</th><th>Status</th><th>Retrieved (UTC)</th></tr>')
    md += ['', '## Source coverage', '']
    for source in result['sources'].values():
        status = source['status'] + (' · cached' if source.get('cached') else '') + (' · ' + source['error'] if source.get('error') else '')
        report.append('<tr><td><a href="' + esc(source['url']) + '">' + esc(source['label']) + '</a></td><td>' + esc(status) + '</td><td>' + esc(source.get('retrieved', 'Unavailable')) + '</td></tr>')
        md.append('- ' + source['label'] + ': ' + status + '; retrieved ' + source.get('retrieved', 'Unavailable'))
    report.append('</table><h2>Further source records</h2><ul>')
    md += ['', '## Further source records', '']
    seen = set()
    for link in result['links']:
        url = link['url']
        if url in seen or not any(x in (urlparse(url).hostname or '') for x in ('gov.', 'europa.eu', 'admin.ch', 'magicport.ai', 'vesselfinder.com')):
            continue
        seen.add(url)
        report.append('<li><a href="' + esc(url) + '">' + esc(link['label'] or url) + '</a></li>')
        md.append('- [' + (link['label'] or url) + '](' + url + ')')
    report.append('</ul><p class="muted">OpenSanctions is an aggregator; its underlying sources may overlap GUR and official lists. Public access does not imply unrestricted commercial reuse. This report requires no internet connection to read.</p></main></html>')
    (directory / 'report.html').write_text('\n'.join(report), encoding='utf-8')
    (directory / 'report.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
    return directory


def run_scan(imo, out=None, open_report=True):
    print('\nLooking up IMO ' + imo + ' across public sources...')
    result = collect(imo, progress=lambda message: print(message, flush=True))
    directory = export_scan(result, out)
    count = sum(s['status'] == 'collected' for s in result['sources'].values())
    print('\nCollected ' + str(len(result['records'])) + ' findings from ' + str(count) + ' sources.')
    print('Report: ' + str(directory / 'report.html'))
    if not result['records']:
        print('No findings retrieved. Read Source coverage in the report for network/access failures.')
    if open_report:
        webbrowser.open((directory / 'report.html').as_uri())
    return 0 if result['records'] else 2
