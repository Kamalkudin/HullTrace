"""Standalone, evidence-led assistant for the six maritime OSINT challenges."""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse

KINDS = {"name", "mmsi", "flag", "sanction", "manager", "resolution", "program"}
COVERAGE = {"sanctions", "historical_flags"}


def date(value):
    dt.date.fromisoformat(value)
    return value


def valid_imo(value):
    if not re.fullmatch(r"\d{7}", value) or sum(int(n) * w for n, w in zip(value[:6], range(7, 1, -1))) % 10 != int(value[-1]):
        raise ValueError("IMO must have seven digits and a valid check digit.")
    return value


def fresh_case(imo, as_of):
    return {"version": 1, "imo": valid_imo(imo), "as_of": date(as_of), "sources": {}, "facts": [],
            "coverage": {}, "challenge": {"name_pattern": "Range ????", "manager_date": "2026-03-21",
            "sanctions_start": "2024-10-01", "sanctions_end": "2025-12-31",
            "known_jurisdictions": ["United Kingdom", "European Union", "Switzerland", "Canada", "New Zealand", "Ukraine"]}}


def validate(case):
    if case.get("version") != 1:
        raise ValueError("Unsupported case version.")
    valid_imo(case["imo"])
    date(case["as_of"])
    for sid, source in case["sources"].items():
        if not sid or not source["title"].strip() or not source["publisher"].strip():
            raise ValueError("Each source needs an ID, title, and publisher.")
        parsed = urlparse(source["url"])
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("Source URL must be HTTP(S).")
        date(source["retrieved"])
    ids = set()
    for f in case["facts"]:
        if f["id"] in ids:
            raise ValueError("Duplicate fact ID: " + f["id"])
        ids.add(f["id"])
        if f["kind"] not in KINDS or not f["value"].strip() or not f["excerpt"].strip():
            raise ValueError("Each fact needs a supported kind, value, and source excerpt.")
        if f["source"] not in case["sources"]:
            raise ValueError("Unknown source: " + f["source"])
        if not isinstance(f["reviewed"], bool):
            raise ValueError("reviewed must be true or false.")
        date(f["date"])
        if f.get("status", "reported") not in {"reported", "confirmed", "false", "unverified"}:
            raise ValueError("Invalid status.")
        if f["kind"] == "mmsi" and not re.fullmatch(r"\d{9}", f["value"]):
            raise ValueError("MMSI must contain nine digits.")
        if f["kind"] in {"sanction", "resolution"} and not f.get("jurisdiction"):
            raise ValueError("Sanction/resolution facts need a jurisdiction.")
        if f["kind"] == "manager" and not f.get("role"):
            raise ValueError("Manager facts need a role.")
        if f["kind"] == "program" and f.get("namespace") not in {"agency", "aggregator", "unknown"}:
            raise ValueError("Program facts need namespace agency, aggregator, or unknown.")
    for topic, declaration in case["coverage"].items():
        if topic not in COVERAGE or not declaration["note"].strip():
            raise ValueError("Coverage declarations need a supported topic and explanation.")
    for key in ("manager_date", "sanctions_start", "sanctions_end"):
        date(case["challenge"][key])
    if case["challenge"]["sanctions_start"] > case["challenge"]["sanctions_end"]:
        raise ValueError("Sanctions date range is reversed.")


def read_case(path):
    case = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    validate(case)
    return case


def save(path, case):
    validate(case)
    destination = Path(path)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def unique_value(facts):
    values = {f["value"] for f in facts}
    return next(iter(values)) if len(values) == 1 else None


def flag_name(value):
    return ' '.join(value.split()).title()


def evaluate(case):
    validate(case)
    facts = [f for f in case["facts"] if f["reviewed"] and f["date"] <= case["as_of"]]
    cfg = case["challenge"]
    results = []

    def select(kind, **fields):
        return [f for f in facts if f["kind"] == kind and all(f.get(k) == v for k, v in fields.items())]

    def emit(number, value, evidence, missing, note=""):
        results.append({"challenge": number, "status": "candidate" if value else "needs_evidence",
                        "flag": "flag{" + value + "}" if value else None,
                        "evidence": sorted({f["id"] for f in evidence}), "missing": missing, "note": note})

    mmsis = select("mmsi", scope="current", date=case["as_of"])
    mmsi = unique_value(mmsis)
    # Only '?' is a wildcard: challenge expects precisely four trailing letters.
    pattern = re.escape(cfg["name_pattern"]).replace(r"\?", ".")
    flags = [f for f in select("flag") if re.fullmatch(pattern, f.get("vessel_name", ""), re.I)]
    publishers = {}
    for f in flags:
        publishers.setdefault(flag_name(f["value"]), set()).add(case["sources"][f["source"]]["publisher"].strip().casefold())
    consistent = sorted(v for v, ps in publishers.items() if len(ps) >= 2)
    missing = []
    if not mmsi:
        missing.append("Need one unambiguous MMSI snapshot on the case as-of date.")
    if len(publishers) != 2 or len(consistent) != 2:
        missing.append("Need exactly two flag nations under the matching name, each supported by at least two publishers; reconcile extra claims.")
    emit(1, "_".join([mmsi] + consistent) if not missing else None, mmsis + flags, missing,
         "Annotations remain in evidence but are omitted from the answer. Publisher labels are analyst-supplied; verify independence.")

    uk = select("sanction", jurisdiction="United Kingdom", program="GB-RUS", namespace="authority")
    identifier = unique_value(uk)
    emit(2, identifier.upper() if identifier else None, uk,
         [] if identifier else ["Need an unambiguous authority-issued UK GB-RUS identifier."],
         "Uppercase identifier inside lowercase flag{}; challenge wording is ambiguous about uppercasing the wrapper.")

    sanctions = select("sanction")
    national_sanctions = [f for f in sanctions if f['jurisdiction'] not in {'European Union', 'United Nations'}]
    first = min((f["date"] for f in national_sanctions), default=None)
    first_countries = {f["jurisdiction"] for f in national_sanctions if f["date"] == first}
    eu = [f for f in select("resolution", jurisdiction="European Union") if f['date'].startswith('2024-12-')]
    ch = [f for f in select("resolution", jurisdiction="Switzerland") if f['date'].startswith('2024-12-')]
    resolution = unique_value(eu + ch) if eu and ch else None
    missing = []
    if "sanctions" not in case["coverage"]:
        missing.append("Review sanctions coverage and record a completeness declaration.")
    if len(first_countries) != 1:
        missing.append("Need a unique earliest sanctioning jurisdiction; tied dates remain unresolved.")
    if not resolution:
        missing.append("Need matching resolution codes from both EU and Swiss designation records.")
    country = next(iter(first_countries)) if len(first_countries) == 1 else ""
    emit(3, country.replace(" ", "_") + "_" + resolution if not missing else None,
         sanctions + eu + ch, missing, "Earliest among collected designation dates, conditional on declared coverage; cited resolution and legal instrument remain distinct.")

    managers = [f for f in select("manager", date=cfg["manager_date"]) if f.get("role", "").casefold() in {"commerce manager", "commercial manager"}]
    manager = unique_value(managers)
    poi = [f for f in managers if f.get("classification", "").casefold() == "person of interest"]
    emit(4, manager.replace(" ", "_") if manager and poi else None, managers,
         [] if manager and poi else ["Need the exact commercial manager on the appointment date and a sourced Person of Interest classification."],
         "Spelling, capitalization, and company suffix are preserved.")

    programs = select("program", registry="GUR maritime")
    code = unique_value(programs)
    namespaces = {f.get("namespace") for f in programs}
    agency = code and namespaces == {"agency"}
    emit(5, code if agency else None, programs,
         [] if agency else ["Need evidence that the identifier belongs to the agency registry; an aggregator taxonomy code alone does not establish this."],
         "Recorded codes: " + ", ".join(sorted({f["value"] + " (" + f.get("namespace", "unknown") + ")" for f in programs})))

    current = select("flag", scope="current", date=case["as_of"])
    current_values = {flag_name(f['value']) for f in current}
    current_flag = next(iter(current_values)) if len(current_values) == 1 else None
    historical = select("flag", scope="historical")
    former = {flag_name(f["value"]) for f in historical if flag_name(f["value"]) != current_flag}
    window = [f for f in sanctions if cfg["sanctions_start"] <= f["date"] <= cfg["sanctions_end"]]
    jurisdictions = {f["jurisdiction"] for f in window}
    known = set(cfg["known_jurisdictions"])
    extra = jurisdictions - known
    missing = []
    if not COVERAGE.issubset(case["coverage"]):
        missing.append("Declare sanctions and historical-flag coverage after reviewing the records.")
    if not known.issubset(jurisdictions) or len(extra) != 1:
        missing.append("Need the six named jurisdictions plus exactly one additional jurisdiction within the date window.")
    if not current_flag or not historical:
        missing.append("Need one unambiguous current flag snapshot and dated historical flag records.")
    emit(6, next(iter(extra)).replace(" ", "_") + "_" + str(len(former)) if not missing else None,
         window + current + historical, missing,
         "Distinct former flags (including false/unverified): " + ", ".join(sorted(former)))
    return results


def research_links(case):
    imo = case["imo"]
    queries = [f'"{imo}" vessel name MMSI flag history', f'site.equasis.org "{imo}"',
               f'site.search-uk-sanctions-list.service.gov.uk "{imo}"',
               f'site.eur-lex.europa.eu "{imo}"', f'site.seco.admin.ch "{imo}"',
               f'site.war-sanctions.gur.gov.ua "{imo}"', f'site.opensanctions.org "{imo}"',
               f'"{imo}" "{case["challenge"]["manager_date"]}" manager',
               f'site.dfat.gov.au "{imo}"', f'site.international.gc.ca "{imo}"',
               f'site.mfat.govt.nz "{imo}"', f'site.imo.org "{imo}" false flag']
    return [{"query": q, "url": "https://www.google.com/search?" + urlencode({"q": q})} for q in queries]


def export_case(case, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    results = evaluate(case)
    lines = [f'# Maritime investigation: IMO {case["imo"]}', '', f'As of: {case["as_of"]}', '',
             case.get('description', ''), '',
             'Candidate answers depend on analyst-reviewed evidence and declared coverage. This tool has not independently verified source contents.', '']
    for result in results:
        lines += [f'## Challenge {result["challenge"]}', '', result['flag'] or 'Needs evidence.', '', result['note'], '',
                  'Evidence: ' + (', '.join(result['evidence']) or 'None'), '']
        lines += ["- " + x for x in result['missing']]
        lines.append('')
    lines += ['## Evidence register', '']
    graph = ['digraph Maritime {', '  rankdir=LR;', '  vessel [label=' + json.dumps('IMO ' + case['imo']) + '];']
    for index, f in enumerate(case['facts']):
        source = case['sources'][f['source']]
        lines += [f'### {f["id"]}: {f["kind"]} — {f["value"]}', '',
                  f'Date: {f["date"]}; reviewed: {f["reviewed"]}; status: {f.get("status", "reported")}.', '',
                  f'Source: {source["title"]} ({source["publisher"]}), retrieved {source["retrieved"]}.', source['url'], '',
                  'Excerpt: ' + f['excerpt'], '', 'Fields: `' + json.dumps(f, ensure_ascii=False) + '`', '']
        graph += [f'  n{index} [label=' + json.dumps(f['value']) + '];',
                  f'  vessel -> n{index} [label=' + json.dumps(f'{f["kind"]} / {f["date"]} / {f["id"]} / reviewed={f["reviewed"]}') + '];']
    lines += ['## Coverage declarations', '', '```json', json.dumps(case['coverage'], ensure_ascii=False, indent=2), '```', '']
    graph.append('}')
    (directory / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    (directory / 'graph.dot').write_text('\n'.join(graph) + '\n', encoding='utf-8')
    (directory / 'results.json').write_text(json.dumps(results, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    (directory / 'case.json').write_text(json.dumps(case, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        try:
            value = input('Enter vessel IMO [9299898]: ').strip() or '9299898'
        except (EOFError, KeyboardInterrupt):
            return 1
        argv = ['scan', value]
    elif re.fullmatch(r'\d{7}', argv[0]):
        argv.insert(0, 'scan')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    scan = sub.add_parser('scan', help='One-step public investigation: IMO in, report out')
    scan.add_argument('imo'); scan.add_argument('--out'); scan.add_argument('--no-open', action='store_true')
    init = sub.add_parser('init', help='Create an empty case; never overwrite an existing file')
    init.add_argument('case'); init.add_argument('--imo', required=True); init.add_argument('--as-of', required=True)
    source = sub.add_parser('source', help='Add a source record')
    source.add_argument('case'); source.add_argument('--id', required=True)
    for key in ('url', 'title', 'publisher', 'retrieved'):
        source.add_argument('--' + key, required=True)
    fact = sub.add_parser('fact', help='Record an excerpt-backed claim; mark reviewed only after checking the source')
    fact.add_argument('case')
    for key in ('id', 'kind', 'value', 'source', 'date', 'excerpt'):
        fact.add_argument('--' + key, required=True)
    for key in ('scope', 'vessel-name', 'jurisdiction', 'program', 'namespace', 'role', 'classification', 'registry'):
        fact.add_argument('--' + key)
    fact.add_argument('--status', default='reported'); fact.add_argument('--reviewed', action='store_true')
    coverage = sub.add_parser('coverage', help='Record an analyst completeness declaration')
    coverage.add_argument('case'); coverage.add_argument('topic', choices=sorted(COVERAGE)); coverage.add_argument('--note', required=True)
    for command in ('analyze', 'guide', 'validate'):
        sub.add_parser(command).add_argument('case')
    export = sub.add_parser('export'); export.add_argument('case'); export.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'scan':
            from Maritim.vessel_lookup import run_scan
            return run_scan(valid_imo(args.imo), args.out, not args.no_open)
        if args.command == 'init':
            case = fresh_case(args.imo, args.as_of)
            validate(case)
            with Path(args.case).open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(case, indent=2) + '\n')
            print('Created ' + args.case)
            return 0
        case = read_case(args.case)
        if args.command == 'source':
            if args.id in case['sources']:
                raise ValueError('Source ID already exists; edit the case explicitly to correct it.')
            case['sources'][args.id] = {key: getattr(args, key) for key in ('url', 'title', 'publisher', 'retrieved')}
        elif args.command == 'fact':
            case['facts'].append({key: value for key, value in vars(args).items() if key not in {'case', 'command'} and value is not None})
        elif args.command == 'coverage':
            case['coverage'][args.topic] = {'note': args.note}
        elif args.command == 'analyze':
            print(json.dumps(evaluate(case), ensure_ascii=False, indent=2))
        elif args.command == 'guide':
            print(json.dumps(research_links(case), ensure_ascii=False, indent=2))
        elif args.command == 'export':
            export_case(case, args.out)
            print('Exported report.md, results.json, case.json, and graph.dot to ' + args.out)
        elif args.command == 'validate':
            print('Case structure is valid. Source accuracy has not been verified.')
        if args.command in {'source', 'fact', 'coverage'}:
            save(args.case, case)
            print('Saved ' + args.case)
        return 0
    except (ValueError, KeyError, TypeError, OSError, AttributeError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
