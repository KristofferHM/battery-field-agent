"""battery-field-agent — canonical ingestion for public battery field data.

The public surface is intentionally small: the CLI in :mod:`cli`, and the
reconciliation primitives in :mod:`normalize` that the notebooks and the
diagnostics layer reuse.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .normalize import (  # noqa: F401
    CANONICAL_FIELDS,
    ColumnMap,
    resolve_columns,
    normalize_records,
    infer_sign_convention,
    integrate_capacity,
    validate_record,
)

__all__ = [
    "__version__",
    "CANONICAL_FIELDS",
    "ColumnMap",
    "resolve_columns",
    "normalize_records",
    "infer_sign_convention",
    "integrate_capacity",
    "validate_record",
]
