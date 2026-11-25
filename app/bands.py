"""Concentration-band planning for the monthly TP submission check.

Concentrations are non-negative integers in ug/L. A band plan is declared with
at most twelve strictly increasing positive integer boundaries, which partition
the integers from 0 into consecutive half-open bands::

    band 1   [0, b1)
    band 2   [b1, b2)
    ...
    band N   [b(N-1), +inf)

Each eligible water sample has a current *integer* interval ``[lower, hi]``
(the intersection of the highest revisions of its reports, where ``hi`` is the
largest reachable integer). A sample may be assigned to any band that contains
at least one integer of that interval. Because both the interval and the band
order are contiguous, the assignable bands of one sample form a single
consecutive range ``[first, last]``.

Filling declared band counts is therefore a unit-job scheduling problem: every
sample is a job with a release band and a deadline band, every band has a
capacity. Earliest-deadline-first (always place the sample whose deadline is
nearest) with exact capacities is optimal for this structure.

When the declared total matches the eligible sample count but no assignment
exists, Hall's condition over *consecutive* band ranges pinpoints the shortage:
some contiguous segment ``[i, j]`` has more samples confined to it than the
segment's capacity. Reporting that segment is far more useful than a generic
"infeasible" answer.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .domain import MAX_CONCENTRATION, DomainError, Interval

MAX_BAND_BOUNDARIES = 12


def validate_boundaries(values: Sequence[object]) -> tuple[int, ...]:
    """Validate and freeze the positive, strictly increasing band boundaries."""
    if not isinstance(values, (list, tuple)):
        raise DomainError("boundaries must be a list of integers")
    if len(values) > MAX_BAND_BOUNDARIES:
        raise DomainError(
            f"at most {MAX_BAND_BOUNDARIES} band boundaries are allowed"
        )
    frozen: list[int] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainError(f"boundaries[{index}] must be an integer")
        if value <= 0:
            raise DomainError(f"boundaries[{index}] must be a positive integer")
        if value > MAX_CONCENTRATION:
            raise DomainError(f"boundaries[{index}] exceeds the supported integer range")
        if frozen and value <= frozen[-1]:
            raise DomainError("boundaries must be strictly increasing from 0")
        frozen.append(value)
    return tuple(frozen)


def validate_counts(values: Sequence[object], band_count: int) -> tuple[int, ...]:
    """Validate the declared non-negative sample count of every band."""
    if not isinstance(values, (list, tuple)):
        raise DomainError("counts must be a list of integers")
    if len(values) != band_count:
        raise DomainError(
            f"counts must contain exactly {band_count} entries, one for each of "
            f"the {band_count} bands formed by {band_count - 1} boundaries"
        )
    frozen: list[int] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainError(f"counts[{index}] must be an integer")
        if value < 0:
            raise DomainError(f"counts[{index}] must not be negative")
        if value > MAX_CONCENTRATION:
            raise DomainError(f"counts[{index}] exceeds the supported integer range")
        frozen.append(value)
    return tuple(frozen)


def band_index(value: int, boundaries: Sequence[int]) -> int:
    """Zero-based index of the band that contains one integer concentration."""
    return bisect_right(boundaries, value)


def assignable_band_range(
    interval: Interval, boundaries: Sequence[int]
) -> tuple[int, int]:
    """Zero-based first/last band a sample's current interval can fall into.

    The tenant is that any integer in the interval is a possible true
    concentration, so every band holding at least one of those integers is
    reachable. Integer intervals and ordered bands are both contiguous, hence
    the reachable set is the inclusive range from ``band(lower)`` to
    ``band(largest integer)``.
    """
    if interval.is_empty:
        raise DomainError("an empty interval cannot be assigned to a band")
    return (
        band_index(interval.lower, boundaries),
        band_index(interval.max_integer, boundaries),
    )


def describe_bands(boundaries: Sequence[int]) -> list[dict]:
    """Human-readable, one-based band descriptors (without declared counts)."""
    described: list[dict] = []
    for index in range(len(boundaries) + 1):
        lower = boundaries[index - 1] if index > 0 else 0
        upper = boundaries[index] if index < len(boundaries) else None
        notation = f"[{lower}, {upper})" if upper is not None else f"[{lower}, +inf)"
        described.append(
            {
                "band": index + 1,
                "lower": lower,
                "upper": upper,
                "upper_inclusive": False,
                "notation": notation,
            }
        )
    return described


def describe_band_range(
    first_band: int, last_band: int, boundaries: Sequence[int]
) -> str:
    """Notation of a contiguous zero-based band range."""
    lower = boundaries[first_band - 1] if first_band > 0 else 0
    upper = boundaries[last_band] if last_band < len(boundaries) else None
    return f"[{lower}, {upper})" if upper is not None else f"[{lower}, +inf)"


def plan_assignments(
    windows: Mapping[str, tuple[int, int]], counts: Sequence[int]
) -> Optional[dict[str, int]]:
    """Assign every sample to a band within its range, filling counts exactly.

    ``windows`` maps a sample number to its inclusive zero-based band range.
    Returns ``None`` when no exact assignment exists. Ties on the deadline are
    broken by sample number so the returned plan is deterministic.
    """
    if sum(counts) != len(windows):
        raise ValueError("plan_assignments requires equal total demand and capacity")

    ordered: dict[int, list[tuple[int, str]]] = {}
    for sample_no, (first, last) in windows.items():
        if not 0 <= first <= last < len(counts):
            raise DomainError(
                f"sample {sample_no!r} has an invalid assignable band range"
            )
        ordered.setdefault(first, []).append((last, sample_no))

    pending: list[tuple[int, str]] = []
    assignment: dict[str, int] = {}
    for band, capacity in enumerate(counts):
        for item in ordered.get(band, ()):
            heapq.heappush(pending, item)
        for _ in range(capacity):
            if not pending:
                return None
            deadline, sample_no = heapq.heappop(pending)
            if deadline < band:
                return None
            assignment[sample_no] = band
    if pending:
        return None
    return assignment


@dataclass(frozen=True)
class CapacityShortfall:
    """A contiguous zero-based band range whose confined demand exceeds capacity."""

    first_band: int
    last_band: int
    capacity: int
    samples: tuple[str, ...]

    @property
    def demand(self) -> int:
        return len(self.samples)

    @property
    def deficit(self) -> int:
        return self.demand - self.capacity


def find_capacity_shortfall(
    windows: Mapping[str, tuple[int, int]], counts: Sequence[int]
) -> Optional[CapacityShortfall]:
    """Find a contiguous band segment that cannot hold the samples trapped in it.

    For every segment ``[i, j]`` count the samples whose whole assignable range
    lies inside it ("can only fall in this segment"). Hall's theorem for
    interval structures says an exact assignment exists precisely when that
    demand never exceeds the segment's capacity. The reported segment is the
    shortest witness; ties prefer the larger deficit, then the earlier start.

    Confined demand is read from a two-dimensional table indexed by
    ``(first_band, last_band)``, so the search costs ``O(samples + bands^2)``
    rather than rescanning every sample for every segment.
    """
    band_count = len(counts)
    if band_count == 0:
        return None
    capacity_prefix = [0]
    for count in counts:
        capacity_prefix.append(capacity_prefix[-1] + count)

    # freq[first][last] -> samples with exactly this assignable range.
    freq = [[0] * band_count for _ in range(band_count)]
    for sample_no, (first, last) in windows.items():
        if not 0 <= first <= last < band_count:
            raise DomainError(
                f"sample {sample_no!r} has an invalid assignable band range"
            )
        freq[first][last] += 1

    # confined[i][j] -> samples whose range lies entirely within [i, j].
    confined = [[0] * (band_count + 1) for _ in range(band_count + 1)]
    for first in range(band_count - 1, -1, -1):
        row, below = confined[first], confined[first + 1]
        for last in range(band_count):
            row[last] = (
                below[last]
                + (row[last - 1] if last > 0 else 0)
                - (below[last - 1] if last > 0 else 0)
                + freq[first][last]
            )

    best: Optional[CapacityShortfall] = None
    best_key: Optional[tuple[int, int, int]] = None
    for first in range(band_count):
        for last in range(first, band_count):
            demand = confined[first][last]
            capacity = capacity_prefix[last + 1] - capacity_prefix[first]
            if demand <= capacity:
                continue
            key = (last - first, -(demand - capacity), first)
            if best_key is None or key < best_key:
                best_key = key
                samples = tuple(
                    sorted(
                        sample_no
                        for sample_no, (start, end) in windows.items()
                        if start >= first and end <= last
                    )
                )
                best = CapacityShortfall(first, last, capacity, samples)
    return best
