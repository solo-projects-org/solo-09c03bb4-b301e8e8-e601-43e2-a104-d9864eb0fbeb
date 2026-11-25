"""Band-check service: verify and receipt a per-sample band allocation.

A *band check* answers one submission-time question: can the declared number of
samples in every concentration band be supported by the water samples that are
currently eligible for the month?

Eligibility follows the same committed-state rules as the monthly summary::

    eligible = samples whose sampling month is the requested month
               and whose current interval (intersection of the highest
               revision of each report) is non-empty

Samples awaiting retest (empty intersection) and samples without any report are
excluded. The eligible set and its intervals are read inside one transaction, so
the returned verdict describes a single committed state.

Outcomes:

``filled``
    The declared total equals the eligible count and an exact allocation exists.
    A numbered receipt is stored, including a snapshot of every participating
    sample so the receipt can later be revalidated.
``count_mismatch``
    The declared total differs from the eligible count; the difference is
    reported and nothing is stored.
``infeasible``
    Totals agree, but some contiguous run of bands is too small for the samples
    that can only land there. That segment, its capacity and the trapped sample
    numbers are reported; nothing is stored.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Sequence

from . import db
from .bands import (
    CapacityShortfall,
    assignable_band_range,
    describe_band_range,
    describe_bands,
    find_capacity_shortfall,
    plan_assignments,
    validate_boundaries,
    validate_counts,
)
from .domain import Interval, NotFoundError, validate_month


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Eligible samples and snapshots
# ---------------------------------------------------------------------------


def _eligible_intervals(
    conn: sqlite3.Connection, month: str
) -> dict[str, Interval]:
    """Current interval of every eligible sample, in one snapshot.

    Only ``ok`` sample states have a non-empty intersection. Samples with no
    reports have no state row and are therefore absent ("未出单" samples).
    """
    rows = conn.execute(
        "SELECT s.sample_no, st.lower, st.upper, st.upper_inclusive "
        "FROM sample_state AS st "
        "JOIN samples AS s ON s.sample_no = st.sample_no "
        "WHERE s.sampling_month = ? AND st.status = 'ok' "
        "ORDER BY s.sample_no",
        (month,),
    ).fetchall()
    intervals = {
        row["sample_no"]: Interval(
            row["lower"], row["upper"], bool(row["upper_inclusive"])
        )
        for row in rows
    }
    _check_against_summary(conn, month, len(intervals))
    return intervals


def _check_against_summary(
    conn: sqlite3.Connection, month: str, eligible_count: int
) -> None:
    """Cross-check eligibility against the incrementally maintained summary.

    Both are derived from the same committed state. A disagreement means the
    aggregates are corrupted, so fail loudly instead of issuing a receipt on
    contradictory data (mirrors the summary/retest consistency guards).
    """
    row = conn.execute(
        "SELECT denominator FROM monthly_summary WHERE month = ?", (month,)
    ).fetchone()
    denominator = row["denominator"] if row is not None else 0
    if denominator != eligible_count:
        raise RuntimeError(
            "monthly denominator and eligible band-check samples are inconsistent"
        )


def _windows(
    intervals: dict[str, Interval], boundaries: Sequence[int]
) -> dict[str, tuple[int, int]]:
    return {
        sample_no: assignable_band_range(interval, boundaries)
        for sample_no, interval in intervals.items()
    }


def _band_descriptors(
    boundaries: Sequence[int], counts: Sequence[int]
) -> list[dict]:
    described = describe_bands(boundaries)
    for descriptor, count in zip(described, counts):
        descriptor["declared_count"] = count
    return described


def _assignment_view(
    sample_no: str,
    zero_based_band: int,
    window: tuple[int, int],
    boundaries: Sequence[int],
    interval: Optional[Interval] = None,
) -> dict:
    first, last = window
    view = {
        "sample_no": sample_no,
        "band": zero_based_band + 1,
        "notation": describe_band_range(
            zero_based_band, zero_based_band, boundaries
        ),
        "assignable_first_band": first + 1,
        "assignable_last_band": last + 1,
    }
    if interval is not None:
        view["interval"] = interval.to_dict()
    return view


def _shortfall_view(
    shortfall: CapacityShortfall, boundaries: Sequence[int]
) -> dict:
    return {
        "first_band": shortfall.first_band + 1,
        "last_band": shortfall.last_band + 1,
        "notation": describe_band_range(
            shortfall.first_band, shortfall.last_band, boundaries
        ),
        "capacity": shortfall.capacity,
        "sample_count": shortfall.demand,
        "deficit": shortfall.deficit,
        "sample_nos": list(shortfall.samples),
    }


# ---------------------------------------------------------------------------
# Creating a check
# ---------------------------------------------------------------------------


def _next_check_id(conn: sqlite3.Connection, month: str) -> str:
    # Writes hold BEGIN IMMEDIATE, so the per-month sequence is race free.
    # Parse the stored suffixes rather than using COUNT or lexical MAX so a
    # receipt is never renumbered once the sequence passes 9999.
    prefix = f"BC-{month.replace('-', '')}-"
    highest = 0
    for row in conn.execute(
        "SELECT check_id FROM band_checks WHERE month = ?", (month,)
    ).fetchall():
        suffix = row["check_id"][len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:04d}"


def create_band_check(
    path: str,
    month: str,
    boundaries: Sequence[int],
    counts: Sequence[int],
) -> dict:
    """Run a band check and persist a receipt when the declaration is filled."""
    validate_month(month, "month")
    frozen_boundaries = validate_boundaries(boundaries)
    band_count = len(frozen_boundaries) + 1
    frozen_counts = validate_counts(counts, band_count)

    with db.write_transaction(path) as conn:
        intervals = _eligible_intervals(conn, month)
        windows = _windows(intervals, frozen_boundaries)

        declared_total = sum(frozen_counts)
        eligible_total = len(windows)
        base = {
            "check_id": None,
            "month": month,
            "boundaries": list(frozen_boundaries),
            "counts": list(frozen_counts),
            "bands": _band_descriptors(frozen_boundaries, frozen_counts),
            "declared_total": declared_total,
            "eligible_total": eligible_total,
            "difference": eligible_total - declared_total,
            "assignments": None,
            "shortfall": None,
            "created_at": None,
        }

        if declared_total != eligible_total:
            base["outcome"] = "count_mismatch"
            return base

        plan = plan_assignments(windows, frozen_counts)
        if plan is None:
            shortfall = find_capacity_shortfall(windows, frozen_counts)
            if shortfall is None:  # pragma: no cover - planner/theorem agree
                raise RuntimeError(
                    "band allocation failed without a capacity shortfall"
                )
            base["outcome"] = "infeasible"
            base["shortfall"] = _shortfall_view(shortfall, frozen_boundaries)
            return base

        check_id = _next_check_id(conn, month)
        created_at = _now()
        conn.execute(
            "INSERT INTO band_checks(check_id, month, boundaries, counts, "
            "total_samples, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                check_id,
                month,
                json.dumps(list(frozen_boundaries)),
                json.dumps(list(frozen_counts)),
                eligible_total,
                created_at,
            ),
        )
        conn.executemany(
            "INSERT INTO band_check_samples(check_id, sample_no, band, "
            "first_band, last_band, lower, upper, upper_inclusive) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    check_id,
                    sample_no,
                    plan[sample_no] + 1,
                    windows[sample_no][0] + 1,
                    windows[sample_no][1] + 1,
                    intervals[sample_no].lower,
                    intervals[sample_no].upper,
                    1 if intervals[sample_no].upper_inclusive else 0,
                )
                for sample_no in sorted(plan)
            ],
        )

        base["outcome"] = "filled"
        base["check_id"] = check_id
        base["created_at"] = created_at
        base["assignments"] = [
            _assignment_view(
                sample_no,
                plan[sample_no],
                windows[sample_no],
                frozen_boundaries,
                intervals[sample_no],
            )
            for sample_no in sorted(plan)
        ]
        return base


# ---------------------------------------------------------------------------
# Reading and revalidating a receipt
# ---------------------------------------------------------------------------


def _receipt_row(conn: sqlite3.Connection, check_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT check_id, month, boundaries, counts, total_samples, created_at "
        "FROM band_checks WHERE check_id = ?",
        (check_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"band check receipt {check_id!r} does not exist")
    return row


def _stored_snapshot(
    conn: sqlite3.Connection, check_id: str
) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT sample_no, band, first_band, last_band, lower, upper, "
        "upper_inclusive FROM band_check_samples WHERE check_id = ? "
        "ORDER BY sample_no",
        (check_id,),
    ).fetchall()
    return {row["sample_no"]: row for row in rows}


def _compare_snapshot(
    stored: dict[str, sqlite3.Row],
    windows: dict[str, tuple[int, int]],
) -> dict:
    """Compare a stored receipt snapshot with the current eligible samples.

    Bands are compared one-based, exactly as stored and returned to clients.
    Only the *assignable band range* is compared, not the raw interval: an
    interval that narrows without changing which bands it can fall into keeps
    the receipt usable.
    """
    stored_samples = set(stored)
    current_samples = set(windows)

    added = [
        {
            "sample_no": sample_no,
            "assignable_first_band": windows[sample_no][0] + 1,
            "assignable_last_band": windows[sample_no][1] + 1,
        }
        for sample_no in sorted(current_samples - stored_samples)
    ]
    removed = [
        {
            "sample_no": sample_no,
            "assignable_first_band": stored[sample_no]["first_band"],
            "assignable_last_band": stored[sample_no]["last_band"],
        }
        for sample_no in sorted(stored_samples - current_samples)
    ]
    changed = [
        {
            "sample_no": sample_no,
            "original_first_band": stored[sample_no]["first_band"],
            "original_last_band": stored[sample_no]["last_band"],
            "current_first_band": windows[sample_no][0] + 1,
            "current_last_band": windows[sample_no][1] + 1,
        }
        for sample_no in sorted(stored_samples & current_samples)
        if (
            stored[sample_no]["first_band"] != windows[sample_no][0] + 1
            or stored[sample_no]["last_band"] != windows[sample_no][1] + 1
        )
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def get_band_check(path: str, check_id: str) -> dict:
    """Return the original allocation and whether it still applies.

    The receipt stays ``usable`` only while the month's eligible sample set and
    every sample's assignable band range match the snapshot taken at check
    time; otherwise ``recheck_required`` is true and the differences are listed.
    """
    with db.read_transaction(path) as conn:
        row = _receipt_row(conn, check_id)
        boundaries = json.loads(row["boundaries"])
        counts = json.loads(row["counts"])
        stored = _stored_snapshot(conn, check_id)
        intervals = _eligible_intervals(conn, row["month"])
        windows = _windows(intervals, boundaries)
        changes = _compare_snapshot(stored, windows)

    usable = not (changes["added"] or changes["removed"] or changes["changed"])
    return {
        "check_id": row["check_id"],
        "month": row["month"],
        "boundaries": boundaries,
        "counts": counts,
        "bands": _band_descriptors(boundaries, counts),
        "total_samples": row["total_samples"],
        "created_at": row["created_at"],
        "assignments": [
            {
                "sample_no": sample_no,
                "band": sample["band"],
                "notation": describe_band_range(
                    sample["band"] - 1, sample["band"] - 1, boundaries
                ),
                "assignable_first_band": sample["first_band"],
                "assignable_last_band": sample["last_band"],
                "interval": Interval(
                    sample["lower"],
                    sample["upper"],
                    bool(sample["upper_inclusive"]),
                ).to_dict(),
            }
            for sample_no, sample in sorted(stored.items())
        ],
        "usable": usable,
        "recheck_required": not usable,
        "changes": changes,
    }
