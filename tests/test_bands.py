"""Band-check API: allocation, mismatch, capacity shortfall and receipts."""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import create_app  # noqa: E402
from app import bands  # noqa: E402
from app.bands import (  # noqa: E402
    assignable_band_range,
    find_capacity_shortfall,
    plan_assignments,
)
from app.domain import DomainError, Interval  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tp.sqlite3")


@pytest.fixture()
def client(db_path: str):
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


def create_sample(client, sample_no, month="2026-11"):
    response = client.post(
        "/samples", json={"sample_no": sample_no, "sampling_month": month}
    )
    assert response.status_code == 201, response.text


def report(client, sample_no, report_no, revision, lower, upper):
    response = client.post(
        f"/samples/{sample_no}/reports",
        json={
            "report_no": report_no,
            "revision": revision,
            "result": {"kind": "measured", "lower": lower, "upper": upper},
        },
    )
    assert response.status_code == 201, response.text
    return response


def nondetect(client, sample_no, report_no, revision, limit):
    response = client.post(
        f"/samples/{sample_no}/reports",
        json={
            "report_no": report_no,
            "revision": revision,
            "result": {"kind": "nondetect", "detection_limit": limit},
        },
    )
    assert response.status_code == 201, response.text
    return response


def band_check(client, month="2026-11", boundaries=(), counts=(0,)):
    return client.post(
        "/band-checks",
        json={"month": month, "boundaries": list(boundaries), "counts": list(counts)},
    )


# ---------------------------------------------------------------------------
# Pure band algebra
# ---------------------------------------------------------------------------


def test_band_index_boundaries_are_half_open():
    boundaries = (10, 20, 50)
    assert bands.band_index(0, boundaries) == 0
    assert bands.band_index(9, boundaries) == 0
    assert bands.band_index(10, boundaries) == 1  # [10, 20)
    assert bands.band_index(19, boundaries) == 1
    assert bands.band_index(20, boundaries) == 2
    assert bands.band_index(49, boundaries) == 2
    assert bands.band_index(50, boundaries) == 3  # last band is unbounded
    assert bands.band_index(10**30, boundaries) == 3


def test_assignable_range_from_integer_interval():
    boundaries = (10, 20, 50)
    # A closed interval spanning 5..25 can land in bands 1, 2 and 3.
    assert assignable_band_range(Interval(5, 25, True), boundaries) == (0, 2)
    # A half-open non-detect [0, 10) reaches only integer 9, i.e. band 1.
    assert assignable_band_range(Interval(0, 10, False), boundaries) == (0, 0)
    # [0, 10) vs boundary 10: 10 itself is impossible, so still band 1 only.
    assert assignable_band_range(Interval(0, 11, False), boundaries) == (0, 1)
    assert assignable_band_range(Interval(70, 70, True), boundaries) == (3, 3)


def test_validate_boundaries_rejects_bad_input():
    for bad in ([0], [-5], [10, 10], [20, 10], [True], [1.5], list(range(1, 14))):
        with pytest.raises(DomainError):
            bands.validate_boundaries(bad)
    assert bands.validate_boundaries([]) == ()


def test_plan_assignments_and_shortfall_match_brute_force():
    # Exhaustively compare the EDF planner and Hall witness against brute force.
    import itertools

    def brute(windows, counts):
        slots = [band for band, count in enumerate(counts) for _ in range(count)]
        names = sorted(windows)
        if len(names) != len(slots):
            return None
        order = sorted(names, key=lambda name: (windows[name][1], name))
        used = [False] * len(slots)
        result: dict[str, int] = {}

        def backtrack(position):
            if position == len(order):
                return True
            first, last = windows[order[position]]
            for slot_index, band in enumerate(slots):
                if not used[slot_index] and first <= band <= last:
                    used[slot_index] = True
                    result[order[position]] = band
                    if backtrack(position + 1):
                        return True
                    used[slot_index] = False
                    del result[order[position]]
            return False

        return dict(result) if backtrack(0) else None

    band_count = 3
    ranges = [(i, j) for i in range(band_count) for j in range(i, band_count)]
    for counts in itertools.product(range(3), repeat=band_count):
        total = sum(counts)
        if total > 4:
            continue
        for combo in itertools.product(ranges, repeat=total):
            windows = {f"S{k}": window for k, window in enumerate(combo)}
            expected = brute(windows, counts)
            planned = plan_assignments(windows, counts)
            assert (expected is None) == (planned is None), (counts, windows)
            shortfall = find_capacity_shortfall(windows, counts)
            assert (expected is None) == (shortfall is not None), (counts, windows)
            if planned is not None:
                assert sorted(planned.values()) == sorted(
                    band for band, count in enumerate(counts) for _ in range(count)
                )
                for name, band_ in planned.items():
                    assert windows[name][0] <= band_ <= windows[name][1]
            if shortfall is not None:
                assert shortfall.demand > shortfall.capacity
                assert shortfall.capacity == sum(
                    counts[shortfall.first_band : shortfall.last_band + 1]
                )
                for name in shortfall.samples:
                    assert windows[name][0] >= shortfall.first_band
                    assert windows[name][1] <= shortfall.last_band
                assert set(shortfall.samples) == {
                    name
                    for name, (first, last) in windows.items()
                    if first >= shortfall.first_band and last <= shortfall.last_band
                }


# ---------------------------------------------------------------------------
# Filled checks and receipts
# ---------------------------------------------------------------------------


def test_filled_check_assigns_every_sample_and_stores_receipt(client, tmp_path):
    for sample in ("S1", "S2", "S3", "S4", "S5"):
        create_sample(client, sample)
    report(client, "S1", "R1", 1, 5, 9)  # band 1 only
    report(client, "S2", "R2", 1, 10, 19)  # band 2 only
    report(client, "S3", "R3", 1, 20, 55)  # bands 3..4
    report(client, "S4", "R4", 1, 12, 14)  # band 2 only
    report(client, "S5", "R5", 1, 60, 70)  # band 4 only

    response = band_check(client, boundaries=(10, 20, 50), counts=(1, 2, 1, 1))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "filled"
    assert body["declared_total"] == body["eligible_total"] == 5
    assert body["difference"] == 0
    assert body["shortfall"] is None
    assert body["check_id"].startswith("BC-202611-")

    placed = {item["sample_no"]: item["band"] for item in body["assignments"]}
    assert placed == {"S1": 1, "S2": 2, "S3": 3, "S4": 2, "S5": 4}
    # Per-band declared counts are met exactly.
    from collections import Counter

    assert Counter(placed.values()) == Counter({1: 1, 2: 2, 3: 1, 4: 1})
    # Every assignment lies inside the sample's own assignable range.
    for item in body["assignments"]:
        assert item["assignable_first_band"] <= item["band"] <= item["assignable_last_band"]

    # The receipt is readable and currently usable.
    receipt = client.get(f"/band-checks/{body['check_id']}")
    assert receipt.status_code == 200, receipt.text
    stored = receipt.json()
    assert stored["usable"] is True
    assert stored["recheck_required"] is False
    assert stored["total_samples"] == 5
    assert {(a["sample_no"], a["band"]) for a in stored["assignments"]} == set(
        placed.items()
    )
    assert stored["changes"] == {"added": [], "removed": [], "changed": []}


def test_retest_and_reportless_samples_are_excluded(client):
    create_sample(client, "OK")
    create_sample(client, "RETEST")
    create_sample(client, "NO-REPORT")
    report(client, "OK", "OK-1", 1, 10, 20)
    report(client, "RETEST", "RT-1", 1, 0, 5)
    report(client, "RETEST", "RT-2", 1, 60, 70)  # empty intersection -> retest

    response = band_check(client, boundaries=(30,), counts=(1, 0))
    body = response.json()
    assert body["outcome"] == "filled"
    assert body["eligible_total"] == 1
    assert [a["sample_no"] for a in body["assignments"]] == ["OK"]


def test_count_mismatch_reports_difference_and_stores_nothing(client, db_path):
    for sample in ("S1", "S2", "S3"):
        create_sample(client, sample)
        report(client, sample, f"{sample}-R", 1, 10, 20)

    short = band_check(client, boundaries=(10,), counts=(1, 1))
    assert short.status_code == 200
    body = short.json()
    assert body["outcome"] == "count_mismatch"
    assert body["check_id"] is None
    assert body["assignments"] is None
    assert body["declared_total"] == 2
    assert body["eligible_total"] == 3
    assert body["difference"] == 1  # eligible - declared

    over = band_check(client, boundaries=(10,), counts=(2, 2))
    assert over.json()["difference"] == -1

    # A mismatch leaves no receipt behind.
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM band_checks").fetchone()[0] == 0
    conn.close()


def test_infeasible_reports_short_segment_capacity_and_trapped_samples(client, db_path):
    # Band 1 [0,10) is the only home of A1 and A2; B1 can only live in [10,20);
    # FLEX can live in either of [10,30).
    for sample in ("A1", "A2", "B1", "FLEX"):
        create_sample(client, sample)
    report(client, "A1", "A1-R", 1, 0, 5)
    report(client, "A2", "A2-R", 1, 6, 9)
    report(client, "B1", "B1-R", 1, 12, 18)
    report(client, "FLEX", "FLEX-R", 1, 10, 30)

    response = band_check(client, boundaries=(10, 20), counts=(2, 0, 2))
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "infeasible"
    assert body["check_id"] is None
    assert body["declared_total"] == body["eligible_total"] == 4
    shortfall = body["shortfall"]
    # Band 2 [10, 20) has capacity 0 but B1 can only fall there.
    assert shortfall["first_band"] == 2 and shortfall["last_band"] == 2
    assert shortfall["notation"] == "[10, 20)"
    assert shortfall["capacity"] == 0
    assert shortfall["sample_count"] == 1
    assert shortfall["deficit"] == 1
    assert shortfall["sample_nos"] == ["B1"]

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM band_checks").fetchone()[0] == 0
    conn.close()


def test_infeasible_reports_hall_witness_for_multi_band_segment(client):
    # Sample ranges exercise Hall's condition over a multi-band segment.
    #   L1, L2 -> band 1 only
    #   M1     -> bands 1..2
    #   H1     -> band 3 only
    # With counts (2, 0, 2) the segment [1, 2] has capacity 2 but L1, L2, M1
    # all confine to it (3 > 2).
    for sample in ("L1", "L2", "M1", "H1"):
        create_sample(client, sample)
    report(client, "L1", "L1-R", 1, 0, 1)
    report(client, "L2", "L2-R", 1, 2, 3)
    report(client, "M1", "M1-R", 1, 5, 15)
    report(client, "H1", "H1-R", 1, 40, 45)

    body = band_check(client, boundaries=(10, 20), counts=(2, 0, 2)).json()
    assert body["outcome"] == "infeasible"
    shortfall = body["shortfall"]
    assert shortfall["first_band"] == 1 and shortfall["last_band"] == 2
    assert shortfall["notation"] == "[0, 20)"
    assert shortfall["capacity"] == 2
    assert shortfall["sample_count"] == 3
    assert shortfall["deficit"] == 1
    assert shortfall["sample_nos"] == ["L1", "L2", "M1"]


def test_zero_boundaries_and_empty_month(client):
    empty = band_check(client, boundaries=(), counts=(0,))
    assert empty.status_code == 200
    body = empty.json()
    assert body["outcome"] == "filled"
    assert body["assignments"] == []
    assert body["check_id"]

    create_sample(client, "S1")
    report(client, "S1", "R", 1, 10**6, 10**7)
    single = band_check(client, boundaries=(), counts=(1,)).json()
    assert single["outcome"] == "filled"
    assert single["assignments"][0]["band"] == 1
    assert single["assignments"][0]["notation"] == "[0, +inf)"


def test_maximum_twelve_boundaries_produce_thirteen_bands(client):
    create_sample(client, "S1")
    report(client, "S1", "R", 1, 5, 5)
    boundaries = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]
    counts = [1] + [0] * 12
    body = band_check(client, boundaries=boundaries, counts=counts).json()
    assert body["outcome"] == "filled"
    assert len(body["bands"]) == 13
    assert body["bands"][-1]["upper"] is None
    assert body["bands"][-1]["notation"] == "[120, +inf)"


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_band_check_request_validation(client):
    create_sample(client, "S1")
    report(client, "S1", "R", 1, 1, 2)

    cases = [
        {"month": "2026-13", "boundaries": [], "counts": [0]},
        {"month": "2026-11", "boundaries": [10], "counts": [1]},  # wrong length
        {"month": "2026-11", "boundaries": [10, 10], "counts": [1, 1, 1]},
        {"month": "2026-11", "boundaries": [20, 10], "counts": [1, 1, 1]},
        {"month": "2026-11", "boundaries": [0], "counts": [1, 1]},
        {"month": "2026-11", "boundaries": [-1], "counts": [1, 1]},
        {"month": "2026-11", "boundaries": [10.5], "counts": [1, 1]},
        {"month": "2026-11", "boundaries": [True], "counts": [1, 1]},
        {"month": "2026-11", "boundaries": [10], "counts": [-1, 2]},
        {"month": "2026-11", "boundaries": [10], "counts": [1.0, 0]},
        {"month": "2026-11", "boundaries": list(range(1, 14)), "counts": [0] * 14},
        {"month": "2026-11", "boundaries": [2**63], "counts": [1, 1]},
    ]
    for payload in cases:
        response = client.post("/band-checks", json=payload)
        assert response.status_code == 422, (payload, response.text)


def test_unknown_receipt_is_404(client):
    assert client.get("/band-checks/BC-202611-9999").status_code == 404


# ---------------------------------------------------------------------------
# Receipt revalidation
# ---------------------------------------------------------------------------


def test_receipt_becomes_unusable_after_correction_moves_bands(client):
    for sample in ("S1", "S2"):
        create_sample(client, sample)
    report(client, "S1", "S1-R", 1, 5, 9)  # band 1
    report(client, "S2", "S2-R", 1, 20, 30)  # bands 2..3

    created = band_check(client, boundaries=(10, 50), counts=(1, 1, 0)).json()
    assert created["outcome"] == "filled"
    check_id = created["check_id"]
    assert client.get(f"/band-checks/{check_id}").json()["usable"] is True

    # Correct S2 so it can only fall into band 3: the receipt no longer matches.
    report(client, "S2", "S2-R", 2, 60, 70)
    receipt = client.get(f"/band-checks/{check_id}").json()
    assert receipt["usable"] is False
    assert receipt["recheck_required"] is True
    assert receipt["changes"]["added"] == []
    assert receipt["changes"]["removed"] == []
    assert receipt["changes"]["changed"] == [
        {
            "sample_no": "S2",
            "original_first_band": 2,
            "original_last_band": 2,
            "current_first_band": 3,
            "current_last_band": 3,
        }
    ]
    # The original allocation is still readable.
    assert {a["sample_no"]: a["band"] for a in receipt["assignments"]} == {
        "S1": 1,
        "S2": 2,
    }


def test_correction_that_keeps_assignable_bands_keeps_receipt_usable(client):
    create_sample(client, "S1")
    report(client, "S1", "S1-R", 1, 12, 28)  # spans bands 2..3
    created = band_check(client, boundaries=(10, 20), counts=(0, 1, 0)).json()
    check_id = created["check_id"]
    assert created["assignments"][0]["band"] == 2
    assert created["assignments"][0]["assignable_first_band"] == 2
    assert created["assignments"][0]["assignable_last_band"] == 3

    # Narrow the interval while keeping integers in both bands 2 and 3.
    report(client, "S1", "S1-R", 2, 15, 25)
    receipt = client.get(f"/band-checks/{check_id}").json()
    assert receipt["usable"] is True, receipt["changes"]

    # Widen down into band 1 -> the assignable range grows -> recheck required.
    report(client, "S1", "S1-R", 3, 5, 25)
    receipt = client.get(f"/band-checks/{check_id}").json()
    assert receipt["usable"] is False
    assert receipt["changes"]["changed"] == [
        {
            "sample_no": "S1",
            "original_first_band": 2,
            "original_last_band": 3,
            "current_first_band": 1,
            "current_last_band": 3,
        }
    ]


def test_retest_and_new_samples_mark_receipt_for_recheck(client):
    for sample in ("S1", "S2"):
        create_sample(client, sample)
        report(client, sample, f"{sample}-R", 1, 5, 9)
    # A second report keeps S1's intersection non-empty until it is corrected.
    report(client, "S1", "S1-R2", 1, 5, 9)
    created = band_check(client, boundaries=(10,), counts=(2, 0)).json()
    assert created["outcome"] == "filled"
    check_id = created["check_id"]

    # A new eligible sample appears.
    create_sample(client, "S3")
    report(client, "S3", "S3-R", 1, 5, 5)
    receipt = client.get(f"/band-checks/{check_id}").json()
    assert receipt["usable"] is False
    assert [item["sample_no"] for item in receipt["changes"]["added"]] == ["S3"]

    # Empty intersection -> retest -> removed from eligibility.
    report(client, "S1", "S1-R", 2, 40, 50)
    receipt = client.get(f"/band-checks/{check_id}").json()
    assert receipt["usable"] is False
    assert [item["sample_no"] for item in receipt["changes"]["removed"]] == ["S1"]
    assert [item["sample_no"] for item in receipt["changes"]["added"]] == ["S3"]


def test_receipt_is_scoped_to_its_month(client):
    create_sample(client, "S1", month="2026-11")
    report(client, "S1", "S1-R", 1, 5, 9)
    created = band_check(client, month="2026-11", boundaries=(10,), counts=(1, 0)).json()
    check_id = created["check_id"]

    # Another month's samples must not affect usability.
    create_sample(client, "D1", month="2026-12")
    report(client, "D1", "D1-R", 1, 5, 9)
    assert client.get(f"/band-checks/{check_id}").json()["usable"] is True


def test_receipt_numbering_is_per_month_and_monotonic(client):
    create_sample(client, "S1")
    report(client, "S1", "S1-R", 1, 5, 9)
    first = band_check(client, boundaries=(10,), counts=(1, 0)).json()
    second = band_check(client, boundaries=(10,), counts=(1, 0)).json()
    assert first["check_id"] != second["check_id"]
    assert first["check_id"].endswith("-0001")
    assert second["check_id"].endswith("-0002")

    create_sample(client, "D1", month="2026-12")
    report(client, "D1", "D1-R", 1, 5, 9)
    december = band_check(client, month="2026-12", boundaries=(10,), counts=(1, 0)).json()
    assert december["check_id"] == "BC-202612-0001"

    # Each receipt returns its own allocation.
    assert client.get(f"/band-checks/{first['check_id']}").json()["month"] == "2026-11"
    assert client.get(f"/band-checks/{december['check_id']}").json()["month"] == "2026-12"


def test_create_app_initializes_band_tables(tmp_path):
    path = str(tmp_path / "tp.sqlite3")
    with TestClient(create_app(path)):
        pass
    conn = sqlite3.connect(path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    conn.close()
    assert {"band_checks", "band_check_samples"} <= tables


def test_database_schema_rejects_inconsistent_receipt_rows(client, db_path):
    create_sample(client, "S1")
    report(client, "S1", "S1-R", 1, 5, 9)
    created = band_check(client, boundaries=(10,), counts=(1, 0)).json()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO band_check_samples VALUES (?, 'X', 2, 1, 1, 0, 5, 1)",
            (created["check_id"],),
        )  # band outside [first_band, last_band]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO band_check_samples VALUES ('missing', 'X', 1, 1, 1, 0, 5, 1)"
        )  # dangling receipt
    conn.close()


def test_concurrent_band_checks_get_unique_receipts(tmp_path: Path):
    path = str(tmp_path / "tp.sqlite3")
    with TestClient(create_app(path)) as client:
        for index in range(6):
            create_sample(client, f"S{index}")
            report(client, f"S{index}", f"S{index}-R", 1, 5, 9)

    from app import band_service

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(
                band_service.create_band_check(path, "2026-11", (10,), (6, 0))
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    ids = [result["check_id"] for result in results]
    assert len(set(ids)) == len(ids) == 8
    assert all(result["outcome"] == "filled" for result in results)


def test_corrupted_denominator_is_detected(client, db_path):
    create_sample(client, "S1")
    report(client, "S1", "S1-R", 1, 5, 9)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE monthly_summary SET denominator = 5 WHERE month = '2026-11'")
    conn.commit()
    conn.close()
    # The band check derives eligibility from sample_state; a mismatch with the
    # incrementally maintained denominator must fail loudly rather than vouch
    # for contradictory data (an unhandled error becomes a 500 in production).
    with pytest.raises(RuntimeError):
        band_check(client, boundaries=(10,), counts=(1, 0))


def test_openapi_documents_band_checks_with_examples(client):
    spec = client.get("/openapi.json").json()
    assert "/band-checks" in spec["paths"]
    assert "/band-checks/{check_id}" in spec["paths"]
    post = spec["paths"]["/band-checks"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"]
    assert spec["components"]["schemas"]["BandCheckRequest"]["examples"]
    # The receipt read exposes the check id as a path parameter.
    parameters = spec["paths"]["/band-checks/{check_id}"]["get"]["parameters"]
    assert any(p["name"] == "check_id" and p["in"] == "path" for p in parameters)
    assert client.get("/docs").status_code == 200
