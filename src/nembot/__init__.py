"""Load public AEMO MMS monthly data into DuckDB."""
from .ingest import load_mms

__all__ = ["load_mms"]
