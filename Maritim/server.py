"""HullTrace: loopback-only web interface for the vessel lookup and historical records."""
import argparse
import csv
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import urllib.request
import uuid
from urllib.parse import unquote, urlparse
import webbrowser

from Maritim import vessel_lookup
from Maritim import historical_records
from Maritim.case_cli import valid_imo

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'web'
REPORT_ID = re.compile(r'^\d{7}/[A-Za-z0-9_-]+$')


class BusyError(Exception):
    pass


class Application:
    def __init__(self, output=None, collect=None):
        self.output = Path(output or ROOT / 'output' / 'scans').resolve()
        self.collect = collect or vessel_lookup.collect
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None

    def report_path(self, report_id):
        if not REPORT_ID.fullmatch(report_id):
            raise ValueError('Invalid report identifier.')
        path = (self.output / report_id).resolve()
        if not path.is_relative_to(self.output):
            raise ValueError('Invalid report path.')
        return path

    def read_report(self, report_id):
        data = json.loads((self.report_path(report_id) / 'data.json').read_text(encoding='utf-8'))
        data['report_id'] = report_id
        data['challenge_leads'] = vessel_lookup.challenge_leads(data)
        return data

    def history(self):
        result = []
        paths = sorted(self.output.glob('*/*/data.json'), key=lambda p: p.parent.name, reverse=True)
        for path in paths[:100]:
            try:
                report_id = path.parent.relative_to(self.output).as_posix()
                data = self.read_report(report_id)
                name = next((r['value'] for r in data['records'] if r['label'] == 'Reported vessel name'), 'Unnamed vessel')
                result.append({'id': report_id, 'imo': data['imo'], 'name': name, 'generated_at': data['generated_at'],
                               'observations': len(data['records']), 'sources': sum(s['status'] == 'collected' for s in data['sources'].values())})
            except (ValueError, KeyError, OSError, TypeError):
                continue
        return result

    def start(self, imo):
        valid_imo(imo)
        with self.lock:
            if self.active:
                raise BusyError('A lookup is already running. Please wait for it to finish.')
            if len(self.jobs) >= 100:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {'id': job_id, 'imo': imo, 'state': 'running', 'events': [], 'report_id': None}
            self.active = job_id
        threading.Thread(target=self._run, args=(job_id, imo), daemon=True).start()
        return job_id

    def status(self, job_id):
        with self.lock:
            return json.loads(json.dumps(self.jobs[job_id]))

    def _run(self, job_id, imo):
        def progress(message):
            with self.lock:
                self.jobs[job_id]['events'].append(str(message))
        try:
            progress('Checking public vessel records')
            result = self.collect(imo, progress=progress)
            run = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + job_id[:6]
            report_id = imo + '/' + run
            vessel_lookup.export_scan(result, self.report_path(report_id))
            with self.lock:
                self.jobs[job_id].update(state='complete', report_id=report_id,
                                         observations=len(result['records']))
        except Exception as error:
            with self.lock:
                self.jobs[job_id].update(state='failed', error=str(error))
        finally:
            with self.lock:
                self.active = None


class Handler(BaseHTTPRequestHandler):
    server_version = 'HullTrace/1.0'

    def log_message(self, fmt, *args):
        pass

    def send(self, status, content, content_type='application/json; charset=utf-8', filename=None):
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False).encode('utf-8')
        elif isinstance(content, str):
            content = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self, mutation=False):
        port = self.server.server_port
        hosts = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}
        origins = {'http://' + host for host in hosts}
        name = os.environ.get('CODESPACE_NAME')
        domain = os.environ.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN')
        if os.environ.get('CODESPACES') == 'true' and name and domain:
            forwarded_host = f'{name}-{port}.{domain}'
            hosts.add(forwarded_host)
            origins.add('https://' + forwarded_host)
        if self.headers.get('Host') not in hosts:
            self.send(403, {'error': 'Unrecognized workspace host.'})
            return False
        if mutation:
            origin = self.headers.get('Origin')
            if (origin and origin not in origins) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send(403, {'error': 'Cross-origin requests are not accepted.'})
                return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = unquote(urlparse(self.path).path)
        app = self.server.app
        try:
            if path == '/qualifier':
                self.send_response(302)
                self.send_header('Location', '/records')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            pages = {'/records': 'records.html', '/saved': 'saved.html'}
            if path in pages:
                self.send(200, (STATIC / pages[path]).read_bytes(), 'text/html; charset=utf-8')
            elif path in {'/evidence/' + name for name in (*historical_records.PHOTOS, 'starship-source.jpg')}:
                self.send(200, (STATIC / path[1:]).read_bytes(), 'image/png' if path.endswith('.png') else 'image/jpeg')
            elif path in {'/', '/app.js', '/records.js', '/style.css', '/favicon.svg'}:
                name = 'index.html' if path == '/' else path[1:]
                kind = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}[Path(name).suffix]
                self.send(200, (STATIC / name).read_bytes(), kind + '; charset=utf-8')
            elif path == '/api/history':
                self.send(200, {'reports': app.history(), 'active_job': app.active})
            elif path.startswith('/api/jobs/'):
                self.send(200, app.status(path[len('/api/jobs/'):]))
            elif path.startswith('/api/reports/'):
                self.send(200, app.read_report(path[len('/api/reports/'):]))
            elif path.startswith('/reports/'):
                report_id, _, filename = path[len('/reports/'):].rpartition('/')
                types = {'report.html': 'text/html', 'report.md': 'text/markdown', 'data.json': 'application/json'}
                if filename not in types:
                    raise FileNotFoundError()
                self.send(200, (app.report_path(report_id) / filename).read_bytes(), types[filename] + '; charset=utf-8',
                          filename if filename != 'report.html' else None)
            else:
                self.send(404, {'error': 'Not found.'})
        except (FileNotFoundError, KeyError):
            self.send(404, {'error': 'The requested report or lookup was not found.'})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request or saved report.'})
        except OSError:
            self.send(500, {'error': 'The saved file could not be read.'})

    def do_POST(self):
        if not self.trusted(mutation=True):
            return
        path = urlparse(self.path).path
        if path not in {'/api/scans', '/api/qualifier', '/api/records'}:
            self.send(404, {'error': 'Not found.'})
            return
        expected_type = 'text/csv' if path == '/api/records' else 'application/json'
        if self.headers.get('Content-Type', '').split(';')[0].strip() != expected_type:
            self.send(415, {'error': 'Expected ' + expected_type + '.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if path == '/api/records':
                if length > historical_records.CSV_MAX_BYTES:
                    self.send(413, {'error': 'Choose a CSV file of up to 1 MB (1,048,576 bytes).'})
                    return
                if length <= 0:
                    raise ValueError('Choose a CSV with at least one record.')
                try:
                    content = self.rfile.read(length).decode('utf-8-sig')
                except UnicodeDecodeError:
                    raise ValueError('Save the CSV as UTF-8 and try again.') from None
                rows = historical_records.import_history(content)
                if not rows:
                    raise ValueError('The CSV must contain at least one record.')
                self.send(200, {'records': [{'kind': kind, 'source': row['url'], **row['fields']}
                                             for kind, row in rows]})
                return
            if not 0 < length <= (131072 if path == '/api/qualifier' else 4096):
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(length))
            if path == '/api/qualifier' and isinstance(data, dict):
                clues = data.get('clues')
                if not isinstance(clues, list) or not 1 <= len(clues) <= 5:
                    raise ValueError('Supply one to five maritime clues.')
                imported = historical_records.import_history(data.get('records', ''))
                self.send(200, {'cases': [historical_records.investigate(clue, data.get('assisted'), imported) for clue in clues]})
                return
            if not isinstance(data, dict) or not isinstance(data.get('imo'), str):
                raise ValueError('Enter a seven-digit IMO number.')
            job_id = self.server.app.start(data['imo'].strip())
            self.send(202, {'job_id': job_id})
        except BusyError as error:
            self.send(409, {'error': str(error)})
        except (ValueError, TypeError, csv.Error) as error:
            self.send(400, {'error': str(error)})


class WorkspaceServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR can let two active servers listen on the same port.
    allow_reuse_address = os.name != 'nt'


def make_server(port=8765, app=None):
    server = WorkspaceServer(('127.0.0.1', port), Handler)
    server.app = app or Application()
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    try:
        server = make_server(args.port)
    except OSError:
        if not args.no_open:
            url = f'http://127.0.0.1:{args.port}/'
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.headers.get('Server', '').split()[:1] == [Handler.server_version]:
                        webbrowser.open(url)
                        return 0
            except OSError:
                pass
        print(f'Port {args.port} is in use. Try: python -m Maritim.server --port {args.port + 1}')
        return 1
    url = f'http://127.0.0.1:{server.server_port}'
    print('HullTrace is running at ' + url, flush=True)
    print('Keep this window open. Press Ctrl+C to stop.', flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
