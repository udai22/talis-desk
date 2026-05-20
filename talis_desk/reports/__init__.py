"""Six-stage adversarial research-report pipeline + persistence.

This sub-package owns the 6-stage pipeline that turns every surviving
hypothesis into a SERIOUS institutional research report:

  Stage 0 — Dossier pull (tic.db reads, no LLM)
  Stage 1 — Comparables (1 Sonnet call)
  Stage 2 — Researcher draft (Opus)
  Stage 3 — Dual adversarial critic (Opus x 2, in parallel)
  Stage 4 — Iterative improve loop (<=3 turns)
  Stage 5 — Copy-edit polish (Haiku)
  Stage 6 — Grade & score (Sonnet)

The daily brief composes from these report rows (research_reports
table) instead of stitching together raw hypotheses + ideas.

Public surface:
  * `ResearchReport`              — the persisted artifact (dataclass).
  * `ReportKind`, `AdversarialSeverity` — string literals.
  * `new_report_id()`             — `rpt_<hex>` generator.
  * `emit_research_report`        — bitemporal append-only writer.
  * `fetch_reports_for_cycle`     — read helper for the brief composer.
  * `fetch_reports_by_kind`       — ad-hoc audit read helper.
  * `run_report_pipeline`         — the 6-stage adversarial chain.
  * `ResearchPipelineResult`      — return shape of the pipeline.
  * `ReportPipelineUnavailableError` — raised when the researcher
                                       draft stage exhausts all providers.
  * `EvidenceDossier`, `pull_dossier`, `render_dossier_markdown`
  * `ComparablesPack`, `ComparablesUnavailableError`,
    `find_comparables`, `render_comparables_markdown`
  * `polish_prose`                — copy-edit pass (soft-fails).
  * `ReportGrade`, `grade_report`, `DEFAULT_GRADE_THRESHOLD`
"""
from __future__ import annotations

from .comparables import (
    ComparablesPack,
    ComparablesUnavailableError,
    find_comparables,
    render_comparables_markdown,
)
from .dossier import (
    EvidenceDossier,
    pull_dossier,
    render_dossier_markdown,
)
from .grade import DEFAULT_GRADE_THRESHOLD, ReportGrade, grade_report
from .model import (
    AdversarialSeverity,
    ReportKind,
    ResearchReport,
    new_report_id,
)
from .persist import (
    emit_research_report,
    fetch_reports_by_kind,
    fetch_reports_for_cycle,
)
from .pipeline import (
    ReportPipelineUnavailableError,
    ResearchPipelineResult,
    run_report_pipeline,
)
from .polish import polish_prose

__all__ = [
    # core
    "ResearchReport",
    "ReportKind",
    "AdversarialSeverity",
    "new_report_id",
    "emit_research_report",
    "fetch_reports_for_cycle",
    "fetch_reports_by_kind",
    # pipeline
    "run_report_pipeline",
    "ResearchPipelineResult",
    "ReportPipelineUnavailableError",
    # stage helpers
    "EvidenceDossier",
    "pull_dossier",
    "render_dossier_markdown",
    "ComparablesPack",
    "ComparablesUnavailableError",
    "find_comparables",
    "render_comparables_markdown",
    "polish_prose",
    "ReportGrade",
    "grade_report",
    "DEFAULT_GRADE_THRESHOLD",
]
