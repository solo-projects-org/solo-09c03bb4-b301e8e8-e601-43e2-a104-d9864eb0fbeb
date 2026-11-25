"""FastAPI application for the single-section total-phosphorus check."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional, Union

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator

from . import band_service, db, service
from .bands import MAX_BAND_BOUNDARIES, MAX_CONCENTRATION
from .domain import ConflictError, DomainError, NotFoundError, ReportContent


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SampleCreate(BaseModel):
    sample_no: str = Field(..., min_length=1, max_length=128, examples=["S-2026-001"])
    sampling_month: str = Field(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-11"],
        description="Fixed sampling month; cannot change after the sample is created.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"sample_no": "S-2026-001", "sampling_month": "2026-11"}]
        }
    }


class MeasuredResult(BaseModel):
    kind: Literal["measured"] = "measured"
    lower: int = Field(..., ge=0, examples=[30], description="Inclusive lower bound, ug/L.")
    upper: int = Field(..., ge=0, examples=[45], description="Inclusive upper bound, ug/L.")

    @model_validator(mode="after")
    def _ordered(self) -> "MeasuredResult":
        if self.lower > self.upper:
            raise ValueError("lower must be <= upper")
        return self


class NonDetectResult(BaseModel):
    kind: Literal["nondetect"] = "nondetect"
    detection_limit: int = Field(
        ...,
        gt=0,
        examples=[10],
        description="Detection limit L, ug/L. Interpreted as the interval [0, L).",
    )


ReportResult = Union[MeasuredResult, NonDetectResult]


class ReportSubmit(BaseModel):
    report_no: str = Field(..., min_length=1, max_length=128, examples=["LAB-A-77"])
    revision: int = Field(..., ge=1, examples=[1])
    result: ReportResult = Field(
        ...,
        discriminator="kind",
        description=(
            "A measured closed interval [lower, upper] or a non-detect limit L "
            "meaning [0, L)."
        ),
    )
    sampling_month: Optional[str] = Field(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=[None],
        description=(
            "Optional month used only when this request creates the sample; it must "
            "match the sample's fixed month otherwise."
        ),
    )

    def content(self) -> ReportContent:
        if self.result.kind == "measured":
            return ReportContent.measured(self.result.lower, self.result.upper)
        return ReportContent.nondetect(self.result.detection_limit)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "report_no": "LAB-A-77",
                    "revision": 1,
                    "result": {"kind": "measured", "lower": 30, "upper": 45},
                },
                {
                    "report_no": "LAB-B-12",
                    "revision": 2,
                    "result": {"kind": "nondetect", "detection_limit": 10},
                },
            ]
        }
    }


class ErrorBody(BaseModel):
    detail: str


class IntervalOut(BaseModel):
    lower: int
    upper: int
    upper_inclusive: bool
    notation: str


class ReportOut(BaseModel):
    report_no: str
    revision: int
    result: dict
    is_current: bool
    fingerprint: str
    created_at: str


class SampleOut(BaseModel):
    sample_no: str
    sampling_month: str
    created_at: str
    status: Literal["ok", "retest", "no_reports"]
    current_interval: Optional[IntervalOut]
    current_revisions: dict[str, int]
    reports: list[ReportOut]
    updated_at: Optional[str]


class ReportIngestOut(BaseModel):
    accepted: bool
    idempotent: bool
    report_no: str
    revision: int
    sample: SampleOut


class SummaryOut(BaseModel):
    month: str
    threshold: int
    total_samples: int = Field(..., description="Samples with at least one lab report.")
    denominator: int = Field(
        ..., description="Samples whose current interval is non-empty."
    )
    at_or_above_lower_bound: int = Field(
        ...,
        description=(
            "Samples that certainly reach or exceed the warning line: every value in "
            "their current interval is >= threshold."
        ),
    )
    at_or_above_upper_bound: int = Field(
        ...,
        description=(
            "Samples that may reach or exceed the warning line: at least one value in "
            "their current interval is >= threshold."
        ),
    )
    retest_count: int
    retest_samples: list[str]
    calculated_at: str


class RetestOut(BaseModel):
    month: str
    retest_count: int
    retest_samples: list[str]


# --- Band check -------------------------------------------------------------

BandBoundary = Annotated[
    StrictInt,
    Field(gt=0, le=MAX_CONCENTRATION, examples=[10]),
]
BandCount = Annotated[
    StrictInt,
    Field(ge=0, le=MAX_CONCENTRATION, examples=[1]),
]


class BandCheckRequest(BaseModel):
    month: str = Field(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-11"],
        description="Sampling month whose committed samples are checked.",
    )
    boundaries: list[BandBoundary] = Field(
        default_factory=list,
        max_length=MAX_BAND_BOUNDARIES,
        examples=[[10, 20, 50]],
        description=(
            "Up to 12 strictly increasing positive integer boundaries in ug/L. "
            "They split concentrations from 0 into left-closed, right-open bands "
            "[0, b1), [b1, b2), ..., [bN, +inf)."
        ),
    )
    counts: list[BandCount] = Field(
        ...,
        examples=[[1, 2, 0, 0]],
        description=(
            "Declared non-negative sample count of each band, in order. Must hold "
            "exactly len(boundaries) + 1 entries (the last band has no upper bound)."
        ),
    )

    @model_validator(mode="after")
    def _consistent(self) -> "BandCheckRequest":
        if any(
            right <= left
            for left, right in zip(self.boundaries, self.boundaries[1:])
        ):
            raise ValueError("boundaries must be strictly increasing")
        expected = len(self.boundaries) + 1
        if len(self.counts) != expected:
            raise ValueError(
                f"counts must contain exactly {expected} entries for "
                f"{len(self.boundaries)} boundaries"
            )
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "month": "2026-11",
                    "boundaries": [10, 20, 50],
                    "counts": [1, 2, 0, 0],
                },
                {
                    "month": "2026-11",
                    "boundaries": [],
                    "counts": [3],
                },
            ]
        }
    }


class BandOut(BaseModel):
    band: int = Field(..., description="One-based band number.")
    lower: int
    upper: Optional[int] = Field(
        None, description="Exclusive upper boundary; null for the last band."
    )
    upper_inclusive: bool = Field(
        False, description="Always false: every band is left-closed, right-open."
    )
    notation: str
    declared_count: int


class AssignmentOut(BaseModel):
    sample_no: str
    band: int = Field(..., description="One-based band this sample is placed in.")
    notation: str = Field(..., description="Notation of the assigned band.")
    assignable_first_band: int
    assignable_last_band: int
    interval: Optional[IntervalOut] = Field(
        default=None,
        description="Snapshot of the sample's current interval at check time.",
    )


class ShortfallOut(BaseModel):
    first_band: int = Field(..., description="First band of the short segment (1-based).")
    last_band: int = Field(..., description="Last band of the short segment (1-based).")
    notation: str
    capacity: int = Field(..., description="Declared samples the segment can hold.")
    sample_count: int = Field(
        ..., description="Samples that can only fall inside this segment."
    )
    deficit: int = Field(..., description="sample_count - capacity.")
    sample_nos: list[str] = Field(
        ..., description="Samples whose whole assignable range is inside the segment."
    )


class BandCheckOut(BaseModel):
    outcome: Literal["filled", "count_mismatch", "infeasible"]
    check_id: Optional[str] = Field(
        None, description="Receipt number; present only when outcome is filled."
    )
    month: str
    boundaries: list[int]
    counts: list[int]
    bands: list[BandOut]
    declared_total: int
    eligible_total: int = Field(
        ...,
        description=(
            "Samples for the month whose current interval is non-empty. "
            "Retest and report-less samples are excluded."
        ),
    )
    difference: int = Field(
        ..., description="eligible_total - declared_total; non-zero only on mismatch."
    )
    assignments: Optional[list[AssignmentOut]]
    shortfall: Optional[ShortfallOut]
    created_at: Optional[str]


class BandChangeSetOut(BaseModel):
    sample_no: str
    assignable_first_band: int
    assignable_last_band: int


class BandChangedSampleOut(BaseModel):
    sample_no: str
    original_first_band: int
    original_last_band: int
    current_first_band: int
    current_last_band: int


class BandChangesOut(BaseModel):
    added: list[BandChangeSetOut] = Field(
        ..., description="Samples now eligible but absent from the receipt."
    )
    removed: list[BandChangeSetOut] = Field(
        ..., description="Samples on the receipt that are no longer eligible."
    )
    changed: list[BandChangedSampleOut] = Field(
        ..., description="Samples whose assignable band range moved."
    )


class BandReceiptOut(BaseModel):
    check_id: str
    month: str
    boundaries: list[int]
    counts: list[int]
    bands: list[BandOut]
    total_samples: int
    created_at: str
    assignments: list[AssignmentOut]
    usable: bool = Field(
        ...,
        description=(
            "True only when the month's eligible samples and every assignable "
            "band range still match the snapshot taken at check time."
        ),
    )
    recheck_required: bool = Field(
        ..., description="Negation of usable; when true, run the check again."
    )
    changes: BandChangesOut


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(db_path: Optional[str] = None) -> FastAPI:
    path = db.database_path(db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.initialize(path)
        yield

    app = FastAPI(
        title="Single-section total phosphorus check",
        version="1.0.0",
        description=(
            "Lab reports declare integer concentration intervals in ug/L "
            "(measured `[lower, upper]`, non-detect `[0, detection_limit)`). "
            "Each report is used at its highest revision; a water sample's current "
            "interval is the intersection of its current report intervals. Empty "
            "intersections are listed for retest and excluded from the denominator. "
            "Band checks verify that a declared per-band sample count can be "
            "supported by the month's eligible samples and issue a receipt."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.DatabaseError)
    async def _db_error(_: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        # The failing transaction has already been rolled back; readers see the
        # last committed state. Never leak storage details to the client.
        logging.getLogger("tp-check").exception("database operation failed")
        return JSONResponse(
            status_code=500,
            content={"detail": "storage failure; previous results remain in effect"},
        )

    @app.get("/healthz", tags=["ops"], summary="Liveness check")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post(
        "/samples",
        response_model=SampleOut,
        status_code=201,
        tags=["samples"],
        summary="Register a water sample with its fixed sampling month",
        responses={409: {"model": ErrorBody, "description": "Month conflict"}},
    )
    def create_sample(body: SampleCreate) -> dict:
        sample, _ = service.create_sample(path, body.sample_no, body.sampling_month)
        return sample

    @app.get(
        "/samples/{sample_no}",
        response_model=SampleOut,
        tags=["samples"],
        summary="Read a sample, its report history and current interval",
        responses={404: {"model": ErrorBody}},
    )
    def get_sample(sample_no: str) -> dict:
        return service.get_sample(path, sample_no)

    @app.post(
        "/samples/{sample_no}/reports",
        response_model=ReportIngestOut,
        status_code=201,
        tags=["reports"],
        summary="Submit or correct one lab report revision",
        description=(
            "A new revision is created with status 201. Re-posting the identical "
            "report number, revision and content is idempotent and returns 200 "
            "without adding a report, re-counting or recomputing the summary."
        ),
        responses={
            200: {
                "model": ReportIngestOut,
                "description": "Exact re-post; nothing changed",
            },
            404: {"model": ErrorBody},
            409: {
                "model": ErrorBody,
                "description": (
                    "Same report number/revision with different content, a stale "
                    "revision, or a report number bound to another sample"
                ),
            },
            422: {"model": ErrorBody, "description": "Invalid interval or month"},
        },
    )
    def submit_report(sample_no: str, body: ReportSubmit, response: Response) -> dict:
        outcome = service.submit_report(
            path,
            sample_no=sample_no,
            report_no=body.report_no,
            revision=body.revision,
            content=body.content(),
            sampling_month=body.sampling_month,
        )
        if outcome["idempotent"]:
            response.status_code = 200
        return outcome

    @app.get(
        "/summary",
        response_model=SummaryOut,
        tags=["summary"],
        summary="Monthly count bounds, denominator and retest list",
        description=(
            "Counts and the retest list are read from one committed SQLite snapshot, "
            "so a concurrent correction cannot make them disagree."
        ),
    )
    def summary(
        month: str = Query(..., examples=["2026-11"], description="Sampling month YYYY-MM."),
        threshold: int = Query(
            ...,
            examples=[20],
            description="Warning line in integer ug/L; reached means >= threshold.",
        ),
    ) -> dict:
        return service.month_summary(path, month, threshold)

    @app.get(
        "/retest",
        response_model=RetestOut,
        tags=["summary"],
        summary="Samples awaiting retest for a month",
    )
    def retest(
        month: str = Query(..., examples=["2026-11"], description="Sampling month YYYY-MM."),
    ) -> dict:
        return service.retest_samples(path, month)

    @app.post(
        "/band-checks",
        response_model=BandCheckOut,
        tags=["band-checks"],
        summary="Verify declared band counts against the month's eligible samples",
        description=(
            "Checks whether the declared sample count of every concentration band "
            "can be supported by the samples currently eligible for the month "
            "(samples with a non-empty current interval; retest and report-less "
            "samples are excluded).\n\n"
            "- `outcome=filled`: an exact per-sample allocation exists and a "
            "numbered receipt (`check_id`) is stored. Each sample is placed in one "
            "band it can fall into and every band reaches its declared count.\n"
            "- `outcome=count_mismatch`: declared total differs from the eligible "
            "count; `difference = eligible_total - declared_total`.\n"
            "- `outcome=infeasible`: totals agree but no allocation exists; "
            "`shortfall` names the contiguous band segment whose capacity is too "
            "small, with the samples that can only land there.\n\n"
            "Band assessment uses integer concentrations: boundaries split values "
            "from 0 into `[0, b1)`, `[b1, b2)`, ..., with no upper bound on the "
            "last band."
        ),
        responses={
            422: {"model": ErrorBody, "description": "Invalid month, boundaries or counts"},
        },
    )
    def create_band_check(body: BandCheckRequest) -> dict:
        return band_service.create_band_check(
            path, body.month, body.boundaries, body.counts
        )

    @app.get(
        "/band-checks/{check_id}",
        response_model=BandReceiptOut,
        tags=["band-checks"],
        summary="Read a band-check receipt and whether it is still usable",
        description=(
            "Returns the original per-sample allocation. `usable` is true only "
            "when the month's currently eligible samples and each sample's "
            "assignable band range still match the snapshot taken when the receipt "
            "was issued; otherwise `recheck_required` is true and `changes` lists "
            "the added, removed and shifted samples."
        ),
        responses={404: {"model": ErrorBody, "description": "Unknown receipt"}},
    )
    def get_band_check(check_id: str) -> dict:
        return band_service.get_band_check(path, check_id)

    app.state.db_path = path
    return app


app = create_app()
