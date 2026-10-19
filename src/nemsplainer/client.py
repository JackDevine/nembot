"""Query helper for generated notebooks: `query(sql)` returns a polars DataFrame.

The web app holds the DuckDB file open for writing, so other processes (a
Jupyter kernel, a marimo editor) can't open it. Queries therefore go to the
running app's `/api/query` endpoint; if the app isn't running, the database
file is opened read-only instead. Results travel as Parquet to keep types.
"""
import io
import json
from pathlib import Path
import tempfile
import urllib.error
import urllib.request

import duckdb
import polars as pl


def parquet_bytes(connection: duckdb.DuckDBPyConnection, sql: str) -> bytes:
    """Run one read-only SELECT and return the result as Parquet bytes."""
    statements = connection.extract_statements(sql)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError('Only one SELECT query is allowed.')
    with tempfile.TemporaryDirectory(prefix='nemsplainer-query-') as directory:
        path = Path(directory) / 'result.parquet'
        connection.execute(f"COPY ({sql.strip().rstrip(';')}) TO '{path}' (FORMAT parquet)")
        return path.read_bytes()


def query(sql: str, url: str = 'http://127.0.0.1:8000', database: str = 'data/nem.duckdb') -> pl.DataFrame:
    request = urllib.request.Request(url.rstrip('/') + '/api/query', data=json.dumps({'sql': sql}).encode(),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return pl.read_parquet(io.BytesIO(response.read()))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get('error')
        except ValueError:
            message = str(exc)
        raise RuntimeError(message) from None
    except urllib.error.URLError:
        pass  # the app isn't running: read the file directly
    with duckdb.connect(database, read_only=True) as connection:
        return pl.read_parquet(io.BytesIO(parquet_bytes(connection, sql)))
