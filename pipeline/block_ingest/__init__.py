"""读入层：本地 Excel + 可选 SQLite → tables[]。不接外部库驱动。"""

from pipeline.block_ingest.loader import (
    IngestError,
    cell_stringify,
    ingest,
    load_excel,
    load_sqlite_one_table,
    measure_table_sizes,
)

__all__ = [
    "IngestError",
    "cell_stringify",
    "ingest",
    "load_excel",
    "load_sqlite_one_table",
    "measure_table_sizes",
]
