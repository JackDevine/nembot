"""Loader schemas for every MMS table, generated from the data-model catalogue.

Column order, DuckDB types and primary keys come from `catalog.json` (built
from AEMO's Electricity Data Model Report PDF). The schemas for the four
original tables were previously hand-written from the HTML report; the
generated ones match them exactly (see tests/test_catalog.py).

The CSV envelope identity (dataset/csv_table in each I record) is not in the
report. It is known for the tables below; for any other table the loader takes
it from the archive's first I record.
"""
from dataclasses import dataclass

from . import catalog


@dataclass(frozen=True)
class Table:
    name: str
    dataset: str | None
    csv_table: str | None
    columns: dict[str, str]
    primary_key: tuple[str, ...]


IDENTITIES = {
    'DISPATCHPRICE': ('DISPATCH', 'PRICE'),
    'DISPATCHREGIONSUM': ('DISPATCH', 'REGIONSUM'),
    'DISPATCH_UNIT_SCADA': ('DISPATCH', 'UNIT_SCADA'),
    'DUDETAILSUMMARY': ('PARTICIPANT_REGISTRATION', 'DUDETAILSUMMARY'),
}
DEFAULT_TABLES = tuple(IDENTITIES)


def _schema(entry: dict) -> Table:
    dataset, csv_table = IDENTITIES.get(entry['name'], (None, None))
    return Table(
        name=entry['name'], dataset=dataset, csv_table=csv_table,
        columns={column['name']: column['type'] for column in entry['columns']},
        primary_key=tuple(entry['primary_key']),
    )


TABLES: dict[str, Table] = {name: _schema(entry) for name, entry in catalog.tables().items()}
