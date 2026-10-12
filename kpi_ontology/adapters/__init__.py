"""Report adapters. Import your own adapter module here (or reference it as
'package.module:Class' in the mapping) so `adapter: <name>` can find it."""
from . import acme_cm  # noqa: F401  (registers AcmeCMWeeklyAdapter)
from .base import ADAPTERS, Extracted, ExtractionResult, ReportAdapter, parse_number, register, resolve_adapter
from .documents import Document, Sheet, load_document

__all__ = ["ADAPTERS", "Extracted", "ExtractionResult", "ReportAdapter", "parse_number", "register",
           "resolve_adapter", "Document", "Sheet", "load_document"]
