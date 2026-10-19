"""Build src/nemsplainer/catalog.json from AEMO's Electricity Data Model Report PDF.

Usage: uv run python scripts/build_catalog.py [--pdf PATH] [--version 5.7]

Downloads the report (unless --pdf is given), parses every table's package,
comment, description, visibility, primary key, indexes and columns, records
which tables the latest MMSDM monthly archive publishes, validates the result
and writes the catalogue. pdfplumber is a dev dependency; the app only reads
the JSON.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse
import urllib.request

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from nemsplainer import nemweb  # noqa: E402
from nemsplainer.catalog import duckdb_type  # noqa: E402

DOCS = 'https://di-help.docs.public.aemo.com.au/Content/Data_Model/'
INDEX = DOCS + 'MMS_Data_Model.htm'
OUTPUT = ROOT / 'src' / 'nemsplainer' / 'catalog.json'

SUBSECTIONS = {
    'Description': 'description', 'Notes': 'notes', 'Primary Key Columns': 'pk',
    'Index Columns': 'index', 'Content': 'content',
}
PARAGRAPH_GAP = 14.5  # points between line tops; larger gaps start a new paragraph


def page_events(page):
    """Text lines outside tables, plus tables, in reading order."""
    tables = page.find_tables()
    boxes = [t.bbox for t in tables]
    events = []
    for line in page.extract_text_lines():
        top = line['top']
        if top < 60 or re.search(r'Page \d+ of \d+$', line['text']):
            continue  # running header and footer
        if any(b[1] - 1 <= top <= b[3] + 1 and b[0] - 1 <= line['x0'] <= b[2] + 1 for b in boxes):
            continue
        events.append((top, 'line', line['text'].strip()))
    for table in tables:
        rows = [[re.sub(r'\s+', ' ', cell or '').strip() for cell in row] for row in table.extract()]
        events.append((table.bbox[1], 'table', rows))
    return sorted(events, key=lambda e: e[0])


class Parser:
    def __init__(self):
        self.packages: dict[str, list[str]] = {}
        self.tables: dict[str, dict] = {}
        self.package = self.table = self.section = None
        self.last_top = None
        self.warnings: list[str] = []

    def paragraph(self, text: str, top: float):
        blocks = self.table['description']
        same = (blocks and blocks[-1]['type'] == 'text' and self.last_top is not None
                and 0 < top - self.last_top <= PARAGRAPH_GAP and not text.startswith('·'))
        if same:
            blocks[-1]['text'] += ' ' + text
        else:
            blocks.append({'type': 'text', 'text': text})
        self.last_top = top

    def line(self, text: str, top: float):
        if not text:
            return
        if match := re.fullmatch(r'\d+ Package: (.+)', text):
            self.package, self.table, self.section = match[1], None, None
            self.packages.setdefault(self.package, [])
            return
        if match := re.fullmatch(r'\d+\.\d+ Table: (.+)', text):
            name = match[1].replace(' ', '')
            self.table = {'name': name, 'package': self.package, 'comment': '', 'description': [],
                          'visibility': None, 'notes': [], 'primary_key': [], 'indexes': [], 'columns': []}
            self.tables[name] = self.table
            self.packages[self.package].append(name)
            self.section = 'head'
            return
        if re.fullmatch(r'\d+\.\d+ (List of tables|Diagram.*)', text):
            self.table = self.section = None
            return
        if self.table is None:
            return
        if (match := re.fullmatch(r'\d+\.\d+\.\d+ (.+)', text)) and (
                match[1] in SUBSECTIONS or match[1].replace(' ', '') == self.table['name']):
            self.section = SUBSECTIONS.get(match[1], 'head')
            self.last_top = None
            return
        if self.section == 'head':
            if text.startswith('Name ') and not self.table['comment']:
                return
            text = text.removeprefix('Comment ') if not self.table['comment'] else text
            self.table['comment'] = (self.table['comment'] + ' ' + text).strip()
        elif self.section == 'description':
            self.paragraph(text, top)
        elif self.section == 'notes':
            if text == 'Name Comment Value':
                return
            if match := re.fullmatch(r'Visibility\s+(.*)', text):
                self.table['visibility'] = match[1].strip() or None
            else:
                self.table['notes'].append(text)
        elif self.section in ('pk', 'index'):
            if text != 'Name':
                key = 'primary_key' if self.section == 'pk' else 'indexes'
                self.table[key].append(text.replace(' ', '').upper())

    def rows(self, rows: list[list[str]]):
        if self.table is None:
            return
        if self.section == 'content':
            for row in rows:
                cells = [c for c in row]
                if not cells or cells[0] == 'Name':
                    continue
                if len(cells) < 4:
                    self.warnings.append(f"{self.table['name']}: unexpected column row {row}")
                    continue
                name, kind, mandatory, comment = cells[0].replace(' ', ''), cells[1].replace(' ', ''), cells[2], cells[-1]
                previous = self.table['columns'][-1] if self.table['columns'] else None
                if not name and not kind and (previous is None or not comment):
                    continue  # blank row, or the tail of a header split by a page break
                if not kind and previous:
                    # A row split by a page break: the name and/or comment continue.
                    previous['display_name'] += name
                    previous['name'] = previous['display_name'].upper()
                    previous['comment'] = (previous['comment'] + ' ' + comment).strip()
                    continue
                # Oracle identifiers are case-insensitive; archives use upper case.
                self.table['columns'].append({'name': name.upper(), 'display_name': name, 'oracle_type': kind,
                                              'type': duckdb_type(kind), 'mandatory': mandatory.upper() == 'X',
                                              'comment': comment})
        elif self.section == 'description':
            cleaned = [[c for c in row if c] for row in rows]
            self.table['description'].append({'type': 'table', 'rows': [r for r in cleaned if r]})
            self.last_top = None


def fetch(url: str, target: Path):
    request = urllib.request.Request(url, headers={'User-Agent': nemweb.USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, target.open('wb') as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)


def archive_tables() -> dict:
    """Tables published in the latest MMSDM monthly archive."""
    latest = nemweb.latest_month(['DISPATCHPRICE'])
    directory = (f'{nemweb.ROOT}{latest.year:04d}/MMSDM_{latest.year:04d}_{latest.month:02d}/'
                 'MMSDM_Historical_Data_SQLLoader/DATA/')
    names = set()
    for url in nemweb._listing(directory):
        filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit('/', 1)[-1]
        if match := re.fullmatch(r'PUBLIC_ARCHIVE#(.+?)#FILE\d+#\d+\.zip', filename, re.IGNORECASE):
            names.add(match[1].upper())
        elif match := re.fullmatch(r'PUBLIC_DVD_(.+?)_\d{12}(?:_FILE\d+)*\.zip', filename, re.IGNORECASE):
            names.add(match[1].upper())
    return {'month': f'{latest.year:04d}-{latest.month:02d}', 'directory': directory, 'tables': sorted(names)}


def validate(parser: Parser) -> list[str]:
    problems = list(parser.warnings)
    for name, table in parser.tables.items():
        columns = [c['name'] for c in table['columns']]
        if not columns:
            problems.append(f'{name}: no columns')
        if len(columns) != len(set(columns)):
            problems.append(f'{name}: duplicate columns')
        missing = set(table['primary_key']) - set(columns)
        if missing:
            problems.append(f'{name}: primary key columns not in table {sorted(missing)}')
        for column in table['columns']:
            if column['type'] is None:
                problems.append(f"{name}.{column['name']}: unmapped type {column['oracle_type']!r}")
            if not re.fullmatch(r'[A-Z][A-Z0-9_#$]*', column['name']):
                problems.append(f"{name}: suspicious column name {column['name']!r}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pdf', type=Path, help='use a local copy of the report')
    parser.add_argument('--version', default='5.7', help='data model version, e.g. 5.7')
    args = parser.parse_args()
    filename = f"Electricity_Data_Model_Report_{args.version.replace('.', '')}.pdf"
    url = DOCS + filename
    with tempfile.TemporaryDirectory() as scratch:
        pdf_path = args.pdf
        if pdf_path is None:
            pdf_path = Path(scratch) / filename
            print(f'Downloading {url}', flush=True)
            fetch(url, pdf_path)
        model = Parser()
        with pdfplumber.open(pdf_path) as pdf:
            first_text = pdf.pages[1].extract_text() or ''
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', first_text)
            version_match = None
            started = False
            for number, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                version_match = version_match or re.search(r'Electricity Data Model v([\d.]+)', text)
                if not started:
                    # Skip the contents pages (dot leaders) until the body begins.
                    started = bool(re.search(r'^1 List of packages\s*$', text, re.MULTILINE))
                    if not started:
                        continue
                for top, kind, value in page_events(page):
                    if kind == 'line':
                        model.line(value, top)
                    else:
                        model.rows(value)
                if number % 100 == 0:
                    print(f'  page {number + 1}/{len(pdf.pages)}: {len(model.tables)} tables', flush=True)
            page_count = len(pdf.pages)
    problems = validate(model)
    archive = archive_tables()
    in_archive = set(archive['tables'])
    for table in model.tables.values():
        table['in_archive'] = table['name'] in in_archive
    catalog = {
        'source': {
            'title': 'Electricity Data Model Report',
            'version': version_match[1] if version_match else args.version,
            'date': datetime.strptime(date_match[1], '%d/%m/%Y').date().isoformat() if date_match else None,
            'url': url, 'index_url': INDEX, 'format': 'pdf', 'pages': page_count,
            'built': datetime.now().astimezone().isoformat(timespec='seconds'),
        },
        'archive': {**archive, 'undocumented': sorted(in_archive - set(model.tables))},
        'packages': [{'name': name, 'tables': tables} for name, tables in model.packages.items()],
        'tables': model.tables,
        'problems': problems,
    }
    OUTPUT.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + '\n')
    documented = sum(t['in_archive'] for t in model.tables.values())
    print(f'{len(model.tables)} tables in {len(model.packages)} packages; {documented} published in the '
          f"{archive['month']} archive; {len(catalog['archive']['undocumented'])} archive tables undocumented; "
          f'{len(problems)} problems')
    for problem in problems[:50]:
        print('  PROBLEM', problem)
    print(f'Wrote {OUTPUT.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
