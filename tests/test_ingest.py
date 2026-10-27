"""Public-loader regressions using real DuckDB and an in-memory NEMWEB server."""

from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date, datetime
from decimal import Decimal
from email.message import Message
from html import escape
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import zipfile

import duckdb

from nembot import load_mms, nemweb


ROOT = "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/"
PRICE_COLUMNS = (
    "SETTLEMENTDATE", "RUNNO", "REGIONID", "DISPATCHINTERVAL", "INTERVENTION",
    "RRP", "ROP", "LASTCHANGED",
)
REGION_COLUMNS = (
    "SETTLEMENTDATE", "RUNNO", "REGIONID", "DISPATCHINTERVAL", "INTERVENTION",
    "TOTALDEMAND", "LASTCHANGED",
)
SCADA_COLUMNS = ("SETTLEMENTDATE", "DUID", "SCADAVALUE", "LASTCHANGED")
DU_COLUMNS = ("DUID", "START_DATE", "END_DATE", "LASTCHANGED")
IDENTITIES = {
    "DISPATCHPRICE": ("DISPATCH", "PRICE", PRICE_COLUMNS),
    "DISPATCHREGIONSUM": ("DISPATCH", "REGIONSUM", REGION_COLUMNS),
    "DISPATCH_UNIT_SCADA": ("DISPATCH", "UNIT_SCADA", SCADA_COLUMNS),
    "DUDETAILSUMMARY": ("PARTICIPANT_REGISTRATION", "DUDETAILSUMMARY", DU_COLUMNS),
}


def mms_csv(name, rows, *, columns=None, version=1, end=True):
    """Produce BOM-prefixed C/I/D CSV, including proper CSV quoting."""
    dataset, csv_table, default_columns = IDENTITIES[name]
    columns = default_columns if columns is None else tuple(columns)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["C", "NEMWEB", "PUBLIC", "MONTHLY"])
    writer.writerow(["I", dataset, csv_table, version, *columns])
    count = 2
    for row in rows:
        writer.writerow(["D", dataset, csv_table, version, *(row.get(c, "") for c in columns)])
        count += 1
    if end:
        writer.writerow(["C", "END OF REPORT", count + 1])
    return output.getvalue().encode("utf-8-sig")


def zip_bytes(members):
    """Create real ZIP bytes; member order is significant for rollback tests."""
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


class ArchiveResponse(BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url
        self.status = 200
        self.headers = Message()
        self.headers["Content-Length"] = str(len(body))

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


class MonthlyArchive:
    """urlopen replacement serving listings and ZIPs, never an actual network."""

    def __init__(self):
        self.files = {}
        self.months = {}
        self.requests = []
        self.downloads = []

    @staticmethod
    def directory(year, month):
        return (
            f"{ROOT}{year}/MMSDM_{year}_{month:02d}/"
            "MMSDM_Historical_Data_SQLLoader/DATA/"
        )

    def add_file(self, name, year, month, rows, *, columns=None, part=1, version=1, members=None):
        directory = self.directory(year, month)
        filename = f"PUBLIC_ARCHIVE#{name}#FILE{part:02d}#{year}{month:02d}010000.zip"
        url = directory + quote(filename)
        if members is None:
            members = {filename[:-4] + ".CSV": mms_csv(name, rows, columns=columns, version=version)}
        self.files[url] = zip_bytes(members)
        self.months.setdefault((year, month), {})[url] = filename
        return url

    @staticmethod
    def canonical(url):
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, quote(unquote(parsed.path), safe="/"), "", ""))

    def open(self, request, timeout=60, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        url = self.canonical(url)
        self.requests.append(url)
        if url in self.files:
            self.downloads.append(url)
            return ArchiveResponse(self.files[url], url)
        links = None
        if url == ROOT:
            links = [(f"{ROOT}{year}/", str(year)) for year in sorted({y for y, _ in self.months})]
        else:
            for year, month in self.months:
                year_url = f"{ROOT}{year}/"
                month_url = f"{year_url}MMSDM_{year}_{month:02d}/"
                loader_url = month_url + "MMSDM_Historical_Data_SQLLoader/"
                if url == year_url:
                    links = [
                        (f"{year_url}MMSDM_{year}_{m:02d}/", f"MMSDM_{year}_{m:02d}")
                        for y, m in sorted(self.months) if y == year
                    ]
                    break
                if url == month_url:
                    links = [(loader_url, "MMSDM_Historical_Data_SQLLoader")]
                    break
                if url == loader_url:
                    links = [(self.directory(year, month), "DATA")]
                    break
                if url == self.directory(year, month):
                    links = list(self.months[year, month].items())
                    break
        if links is None:
            raise HTTPError(url, 404, "Fixture archive not published", Message(), BytesIO())
        body = "<html><body>" + "".join(
            f'<a href="{escape(href, quote=True)}">{escape(label)}</a>' for href, label in links
        ) + "</body></html>"
        return ArchiveResponse(body.encode(), url)


def price_row(timestamp="2024/02/01 00:05:00", **overrides):
    row = {
        "SETTLEMENTDATE": timestamp, "RUNNO": "1", "REGIONID": "NSW1",
        "DISPATCHINTERVAL": "001", "INTERVENTION": "0", "RRP": "42.12345",
        "ROP": "41.00000", "LASTCHANGED": "2024/03/01 00:00:00",
    }
    row.update(overrides)
    return row


class IngestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "store" / "nem.duckdb"
        self.cache = self.root / "cache"
        self.network = MonthlyArchive()
        interval_patch = patch("nembot.nemweb.REQUEST_INTERVAL", 0)
        interval_patch.start()
        self.addCleanup(interval_patch.stop)
        nemweb.clear_cache()
        self.addCleanup(nemweb.clear_cache)
        network_patch = patch("nembot.nemweb._open", side_effect=self.network.open)
        self.urlopen = network_patch.start()
        self.addCleanup(network_patch.stop)
        self.stdout = StringIO()
        self.stderr = StringIO()

    def load(self, tables=("DISPATCHPRICE",), start="2024-02-01", end="2024-02-29", *, database=None):
        # Each load is a separate run; listings are only cached within a process.
        nemweb.clear_cache()
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return load_mms(tables, start, end, database=database or self.database, cache_dir=self.cache)

    def stored(self, query, parameters=None):
        with duckdb.connect(str(self.database)) as connection:
            return connection.execute(query, parameters or []).fetchall()

    def test_shared_connection_survives_spilled_queries_between_loads(self):
        # The web app runs every download on one shared connection. Once DuckDB has spilled to
        # its temp directory, it can't be switched, so loads must not try to set their own.
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row()])
        self.database.parent.mkdir(parents=True)
        shared = duckdb.connect(str(self.database))
        self.addCleanup(shared.close)
        shared.execute("SET memory_limit='64MB'; SET threads=1; SET preserve_insertion_order=false")
        shared.execute("SELECT count(*) FROM (SELECT md5(range::VARCHAR) AS m FROM range(3000000) ORDER BY m)")
        self.assertTrue(Path(str(self.database) + ".tmp").exists())  # it really spilled
        nemweb.clear_cache()
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            load_mms(("DISPATCHPRICE",), "2024-02-01", "2024-02-29", database=self.database, cache_dir=self.cache,
                     connection=shared)
            nemweb.clear_cache()
            load_mms(("DISPATCHPRICE",), "2024-02-01", "2024-02-29", database=self.database, cache_dir=self.cache,
                     connection=shared)
        self.assertEqual(shared.execute("SELECT count(*) FROM DISPATCHPRICE").fetchall(), [(1,)])

    def test_all_tables_multipart_known_subsets_and_typed_values(self):
        first = self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row()])
        second = self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row("2024/02/01 00:10:00", RRP="")], part=2)
        region = price_row(DISPATCHINTERVAL="9999999999999999999999", TOTALDEMAND="12345.67890")
        self.network.add_file("DISPATCHREGIONSUM", 2024, 2, [region])
        self.network.add_file("DISPATCH_UNIT_SCADA", 2024, 2, [{
            "SETTLEMENTDATE": "2024/02/01 00:05:00", "DUID": "UNIT,QUOTED",
            "SCADAVALUE": "-12.345678", "LASTCHANGED": "2024/03/01 00:00:00",
        }])
        self.network.add_file("DUDETAILSUMMARY", 2024, 2, [{
            "DUID": "UNIT,QUOTED", "START_DATE": "2019/01/01 00:00:00",
            "END_DATE": "", "LASTCHANGED": "2024/03/01 00:00:00",
        }])
        with self.load(list(IDENTITIES)) as connection:
            self.assertEqual(connection.execute(
                "SELECT SETTLEMENTDATE, DISPATCHINTERVAL, RRP FROM DISPATCHPRICE ORDER BY SETTLEMENTDATE"
            ).fetchall(), [
                (datetime(2024, 2, 1, 0, 5), "001", Decimal("42.12345")),
                (datetime(2024, 2, 1, 0, 10), "001", None),
            ])
            self.assertEqual(connection.execute(
                "SELECT DISPATCHINTERVAL, TOTALDEMAND FROM DISPATCHREGIONSUM"
            ).fetchone(), (Decimal("9999999999999999999999"), Decimal("12345.67890")))
            self.assertEqual(connection.execute(
                "SELECT DUID, SCADAVALUE FROM DISPATCH_UNIT_SCADA"
            ).fetchone(), ("UNIT,QUOTED", Decimal("-12.345678")))
            self.assertEqual(connection.execute(
                "SELECT DUID, START_DATE, END_DATE FROM DUDETAILSUMMARY"
            ).fetchone(), ("UNIT,QUOTED", datetime(2019, 1, 1), None))
            self.assertEqual(connection.execute("SELECT REGIONID FROM DUDETAILSUMMARY").fetchone(), (None,))
        self.assertCountEqual(self.network.downloads, list(self.network.files))
        self.assertIn(first, self.network.downloads)
        self.assertIn(second, self.network.downloads)

    def test_all_authoritative_regional_key_dimensions_are_preserved(self):
        rows = [price_row(), price_row(INTERVENTION="1"), price_row(RUNNO="2"), price_row(DISPATCHINTERVAL="002")]
        self.network.add_file("DISPATCHPRICE", 2024, 2, rows)
        self.network.add_file("DISPATCHREGIONSUM", 2024, 2, [dict(row, TOTALDEMAND="10") for row in rows])
        with self.load(["dispatchprice", "DISPATCHREGIONSUM", "DISPATCHPRICE"]) as connection:
            for name in ("DISPATCHPRICE", "DISPATCHREGIONSUM"):
                with self.subTest(table=name):
                    self.assertEqual(connection.execute(
                        f"SELECT count(*) FROM {name}"
                    ).fetchone()[0], 4)
                    self.assertEqual(connection.execute(
                        f"SELECT count(DISTINCT INTERVENTION), count(DISTINCT RUNNO), count(DISTINCT DISPATCHINTERVAL) FROM {name}"
                    ).fetchone(), (2, 2, 2))

    def test_newer_corrections_preserve_omitted_columns_but_clear_supplied_nulls(self):
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(LASTCHANGED="")])
        with self.load() as connection:
            self.assertEqual(connection.execute("SELECT RRP, ROP FROM DISPATCHPRICE").fetchone(), (Decimal("42.12345"), Decimal("41")))
        subset = tuple(c for c in reversed(PRICE_COLUMNS) if c != "ROP")
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(RRP="50")], columns=subset, part=2, version=99)
        with self.load() as connection:
            self.assertEqual(connection.execute("SELECT RRP, ROP FROM DISPATCHPRICE").fetchone(), (Decimal("50"), Decimal("41")))
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(RRP="", ROP="", LASTCHANGED="2024/03/02 00:00:00")], part=3)
        with self.load() as connection:
            self.assertEqual(connection.execute("SELECT RRP, ROP FROM DISPATCHPRICE").fetchone(), (None, None))
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(RRP="99", LASTCHANGED="2024/02/28 00:00:00")], part=4)
        with self.load() as connection:
            self.assertEqual(connection.execute("SELECT RRP, ROP FROM DISPATCHPRICE").fetchone(), (None, None))

    def test_identical_ties_collapse_and_conflicting_stored_tie_rolls_back_file(self):
        good_url = self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(), price_row()])
        with self.load() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM DISPATCHPRICE").fetchone()[0], 1)
        self.network.add_file("DISPATCHPRICE", 2024, 2, [
            price_row("2024/02/01 00:10:00"), price_row(RRP="999"),
        ], part=2)
        with self.assertRaises((ValueError, RuntimeError, duckdb.Error)):
            self.load()
        self.assertEqual(self.stored("SELECT SETTLEMENTDATE, RRP FROM DISPATCHPRICE"), [(datetime(2024, 2, 1, 0, 5), Decimal("42.12345"))])
        self.assertEqual(self.stored("SELECT url FROM _nembot.files"), [(good_url,)])

    def test_conflicting_ties_within_one_file_do_not_commit(self):
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(), price_row(RRP="999")])
        with self.assertRaises((ValueError, RuntimeError, duckdb.Error)):
            self.load()
        self.assertEqual(self.stored("SELECT count(*) FROM DISPATCHPRICE"), [(0,)])
        self.assertEqual(self.stored("SELECT count(*) FROM _nembot.files"), [(0,)])

    def test_invalid_selected_data_rolls_back_without_completion(self):
        valid = mms_csv("DISPATCHPRICE", [price_row()])
        dataset, csv_table, _ = IDENTITIES["DISPATCHPRICE"]
        cases = {
            "unknown column": mms_csv("DISPATCHPRICE", [dict(price_row(), UNKNOWN_FIELD="x")], columns=(*PRICE_COLUMNS, "UNKNOWN_FIELD")),
            "missing key column": mms_csv("DISPATCHPRICE", [price_row()], columns=tuple(c for c in PRICE_COLUMNS if c != "RUNNO")),
            "null key": mms_csv("DISPATCHPRICE", [price_row(REGIONID="")]),
            "bad number": mms_csv("DISPATCHPRICE", [price_row(RRP="not-a-number")]),
            "missing end marker": mms_csv("DISPATCHPRICE", [price_row()], end=False),
            "data before header": (f"D,{dataset},{csv_table},1,unexpected\r\nC,END OF REPORT,2\r\n").encode(),
            "wrong data width": valid.replace(b"42.12345,", b"42.12345,EXTRA,"),
        }
        for index, (label, malformed) in enumerate(cases.items()):
            with self.subTest(case=label):
                self.database = self.root / f"invalid-{index}" / "nem.duckdb"
                self.cache = self.root / f"invalid-cache-{index}"
                self.network.add_file("DISPATCHPRICE", 2024, 2, [], members={
                    "first.csv": valid,
                    "malformed.csv": malformed,
                })
                with self.assertRaises((ValueError, RuntimeError, duckdb.Error)):
                    self.load()
                self.assertEqual(self.stored("SELECT count(*) FROM DISPATCHPRICE"), [(0,)])
                self.assertEqual(self.stored("SELECT count(*) FROM _nembot.files"), [(0,)])

    def test_extra_decimal_places_are_rounded_half_up_and_noted(self):
        # AEMO's 2024 DISPATCHPRICE files carry 6 decimals in a column documented with 5.
        self.network.add_file("DISPATCHPRICE", 2024, 2, [
            price_row("2024/02/01 00:05:00", RRP="42.1234550000000000000"),  # a 6th place: rounds half up
            price_row("2024/02/01 00:10:00", RRP="-1.000004"),  # rounds towards zero
            price_row("2024/02/01 00:15:00", RRP="7.25000000000"),  # extra zeros only: unchanged, not noted
        ])
        with self.load(end="2024-02-01") as connection:
            prices = connection.execute("SELECT RRP FROM DISPATCHPRICE ORDER BY SETTLEMENTDATE").fetchall()
            notes = connection.execute("SELECT table_name, note FROM _nembot.notes").fetchall()
        self.assertEqual([p[0] for p in prices], [Decimal("42.12346"), Decimal("-1.00000"), Decimal("7.25000")])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "DISPATCHPRICE")
        self.assertIn("rounded 2 RRP values to DECIMAL(15,5)", notes[0][1])
        self.assertIn("rounded 2 RRP values", self.stdout.getvalue())

    def test_values_too_big_after_rounding_are_still_rejected(self):
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row(RRP="9999999999.999995")])
        with self.assertRaisesRegex(RuntimeError, "exceeds DECIMAL"):
            self.load(end="2024-02-01")
        self.assertEqual(self.stored("SELECT count(*) FROM _nembot.files"), [(0,)])

    def test_narrow_then_wide_and_fresh_database_reuse_cached_files(self):
        self.network.add_file("DISPATCHPRICE", 2024, 2, [
            price_row("2024/02/01 00:05:00"), price_row("2024/02/02 00:05:00"),
        ])
        with self.load(end="2024-02-01") as connection:
            self.assertEqual(connection.execute("SELECT SETTLEMENTDATE FROM DISPATCHPRICE").fetchall(), [(datetime(2024, 2, 1, 0, 5),)])
        with self.load(end="2024-02-02") as connection:
            expected = [(datetime(2024, 2, 1, 0, 5),), (datetime(2024, 2, 2, 0, 5),)]
            self.assertEqual(connection.execute("SELECT SETTLEMENTDATE FROM DISPATCHPRICE ORDER BY 1").fetchall(), expected)
        with self.load(end="2024-02-02") as connection:
            self.assertEqual(connection.execute("SELECT SETTLEMENTDATE FROM DISPATCHPRICE ORDER BY 1").fetchall(), expected)
        with self.load(end="2024-02-02", database=self.root / "fresh.duckdb") as connection:
            self.assertEqual(connection.execute("SELECT SETTLEMENTDATE FROM DISPATCHPRICE ORDER BY 1").fetchall(), expected)
        self.assertEqual(self.network.downloads, list(self.network.files))
        with self.load(start="2024-02-02", end="2024-02-02") as connection:
            self.assertEqual(connection.execute("SELECT SETTLEMENTDATE FROM DISPATCHPRICE ORDER BY 1").fetchall(), expected)

    def test_reference_old_effective_versions_and_newer_closure_outside_window(self):
        old = {"DUID": "UNIT1", "START_DATE": "2019/01/01 00:00:00", "END_DATE": "", "LASTCHANGED": "2024/03/01 00:00:00"}
        current = {"DUID": "UNIT1", "START_DATE": "2024/02/15 00:00:00", "END_DATE": "", "LASTCHANGED": "2024/03/01 00:00:00"}
        self.network.add_file("DUDETAILSUMMARY", 2024, 2, [old, current])
        with self.load(["DUDETAILSUMMARY"]) as connection:
            self.assertEqual(connection.execute("SELECT START_DATE FROM DUDETAILSUMMARY ORDER BY 1").fetchall(), [(datetime(2019, 1, 1),), (datetime(2024, 2, 15),)])
        closed = dict(old, END_DATE="2024/01/31 00:00:00", LASTCHANGED="2024/03/02 00:00:00")
        self.network.add_file("DUDETAILSUMMARY", 2024, 2, [closed], part=2)
        with self.load(["DUDETAILSUMMARY"]) as connection:
            self.assertEqual(connection.execute("SELECT START_DATE, END_DATE FROM DUDETAILSUMMARY ORDER BY 1").fetchall(), [
                (datetime(2019, 1, 1), datetime(2024, 1, 31)), (datetime(2024, 2, 15), None),
            ])
        with self.load(["DUDETAILSUMMARY"], database=self.root / "fresh.duckdb") as connection:
            self.assertEqual(connection.execute("SELECT START_DATE FROM DUDETAILSUMMARY").fetchall(), [(datetime(2024, 2, 15),)])

    def test_reference_overlap_excludes_touching_boundaries(self):
        rows = [
            {"DUID": "ENDS_AT_START", "START_DATE": "2019/01/01 00:00:00", "END_DATE": "2024/02/01 00:00:00"},
            {"DUID": "STARTS_AT_END", "START_DATE": "2024/03/01 00:00:00", "END_DATE": ""},
            {"DUID": "OVERLAPS", "START_DATE": "2019/01/01 00:00:00", "END_DATE": "2024/02/01 00:05:00"},
        ]
        self.network.add_file("DUDETAILSUMMARY", 2024, 2, rows)
        with self.load(["DUDETAILSUMMARY"]) as connection:
            self.assertEqual(connection.execute("SELECT DUID FROM DUDETAILSUMMARY").fetchall(), [("OVERLAPS",)])

    def test_period_ending_midnight_leap_and_year_boundaries(self):
        cases = [
            (date(2024, 2, 29), "2024/02/29", "2024/03/01"),
            (date(2023, 12, 31), "2023/12/31", "2024/01/01"),
        ]
        for day, lower, upper in cases:
            with self.subTest(day=day):
                self.database = self.root / f"{day}.duckdb"
                self.network.add_file("DISPATCHPRICE", day.year, day.month, [
                    price_row(f"{lower} 00:00:00"), price_row(f"{lower} 00:05:00"),
                    price_row(f"{upper} 00:00:00"), price_row(f"{upper} 00:05:00"),
                ])
                with self.load(start=day, end=day.isoformat()) as connection:
                    self.assertEqual(connection.execute("SELECT SETTLEMENTDATE FROM DISPATCHPRICE ORDER BY 1").fetchall(), [
                        (datetime.strptime(f"{lower} 00:05:00", "%Y/%m/%d %H:%M:%S"),),
                        (datetime.strptime(f"{upper} 00:00:00", "%Y/%m/%d %H:%M:%S"),),
                    ])

    def test_defaults_use_three_consecutive_common_months_across_year(self):
        months = [(2023, 11), (2023, 12), (2024, 1)]
        for year, month in months:
            timestamp = f"{year}/{month:02d}/01 00:05:00"
            self.network.add_file("DISPATCHPRICE", year, month, [price_row(timestamp)])
            self.network.add_file("DISPATCH_UNIT_SCADA", year, month, [{"SETTLEMENTDATE": timestamp, "DUID": "UNIT1", "SCADAVALUE": "1"}])
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row()])
        with self.load(["DISPATCHPRICE", "DISPATCH_UNIT_SCADA"], start=None, end=None) as connection:
            expected = [(datetime(year, month, 1, 0, 5),) for year, month in months]
            for name in ("DISPATCHPRICE", "DISPATCH_UNIT_SCADA"):
                self.assertEqual(connection.execute(f"SELECT SETTLEMENTDATE FROM {name} ORDER BY 1").fetchall(), expected)
        self.assertEqual(len(self.network.downloads), 6)
        self.assertFalse(any("MMSDM_2024_02/" in url for url in self.network.downloads))

    def test_missing_middle_month_fails_before_filesystem_changes(self):
        for month in (1, 3):
            self.network.add_file("DISPATCHPRICE", 2024, month, [price_row(f"2024/{month:02d}/01 00:05:00")])
        for start, end in (("2024-01-01", "2024-03-31"), (None, None)):
            with self.subTest(start=start):
                with self.assertRaises((ValueError, RuntimeError, HTTPError)):
                    self.load(start=start, end=end)
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertEqual(self.network.downloads, [])

    def test_missing_selected_table_fails_before_database_creation(self):
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row()])
        with self.assertRaises((ValueError, RuntimeError, HTTPError)):
            self.load(["DISPATCHPRICE", "DISPATCH_UNIT_SCADA"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.network.downloads, [])

    def test_input_errors_have_no_network_or_filesystem_effects(self):
        cases = [
            {"tables": []}, {"tables": "DISPATCHPRICE"}, {"tables": ["UNKNOWN"]},
            {"tables": ["DISPATCHPRICE", 1]}, {"tables": None},
            {"start_date": "2024-2-01"}, {"start_date": "2024-02-30"},
            {"start_date": datetime(2024, 2, 1)}, {"end_date": datetime(2024, 2, 2)},
            {"start_date": "2024-03-01", "end_date": "2024-02-01"},
            {"end_date": "9999-12-30"}, {"start_date": 20240201},
            {"database": ":memory:"}, {"database": ""}, {"cache_dir": ""},
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                arguments = dict(tables=["DISPATCHPRICE"], start_date="2024-02-01", end_date="2024-02-29", database=self.database, cache_dir=self.cache)
                arguments.update(invalid)
                with self.assertRaises(ValueError):
                    load_mms(**arguments)
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertEqual(self.network.requests, [])

    def test_incompatible_existing_schema_is_not_replaced(self):
        self.database.parent.mkdir(parents=True)
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("CREATE TABLE DISPATCHPRICE (sentinel VARCHAR)")
            connection.execute("INSERT INTO DISPATCHPRICE VALUES ('user history')")
        self.network.add_file("DISPATCHPRICE", 2024, 2, [price_row()])
        with self.assertRaises((ValueError, RuntimeError, duckdb.Error)):
            self.load()
        self.assertEqual(self.stored("SELECT * FROM DISPATCHPRICE"), [("user history",)])


if __name__ == "__main__":
    unittest.main()
