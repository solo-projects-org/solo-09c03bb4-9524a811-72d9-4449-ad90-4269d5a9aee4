"""Joint settlement (联合交账) and working-paper behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "settlement.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def register(
    client: TestClient,
    batch_id: str,
    cartons: list[tuple[str, list[str]]],
) -> dict:
    response = client.post(
        "/batches",
        json={
            "batch_id": batch_id,
            "cartons": [
                {"carton_no": carton_no, "serials": serials}
                for carton_no, serials in cartons
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient, batch_id: str, scan_id: str, serial_no: str, point: str = "P1"):
    response = client.post(
        f"/batches/{batch_id}/scans",
        json={"scan_id": scan_id, "receiving_point": point, "serial_no": serial_no},
    )
    assert response.status_code == 201, response.text
    return response.json()


def active_result_id(client: TestClient, batch_id: str) -> int:
    return client.get(f"/batches/{batch_id}").json()["active_result_id"]


def settle(client: TestClient, entries: list[tuple[str, int]]):
    response = client.post(
        "/settlements",
        json={
            "batches": [
                {"batch_id": batch_id, "result_id": result_id}
                for batch_id, result_id in entries
            ]
        },
    )
    return response


def paper(client: TestClient, batch_id: str | None = None) -> dict:
    path = "/working-paper" if batch_id is None else f"/working-paper/{batch_id}"
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def batch_outcome(body: dict, batch_id: str) -> dict:
    return next(item for item in body["batches"] if item["batch_id"] == batch_id)


# ---------------------------------------------------------------------------
# Working paper baseline
# ---------------------------------------------------------------------------


def test_new_batch_starts_as_whole_batch_draft(client):
    register(client, "A", [("CTN-1", ["S-1", "S-2"])])

    data = paper(client)
    assert data["batch_count"] == 1
    assert data["item_count"] == 2
    assert data["occupied_serial_count"] == 2
    assert data["serial_conflicts"] == []
    batch = data["batches"][0]
    assert batch["batch_id"] == "A"
    assert batch["mode"] == "whole_batch"
    assert batch["result_id"] is None
    assert batch["result_is_current"] is False
    assert [item["serial_no"] for item in batch["items"]] == ["S-1", "S-2"]
    assert all(item["receiving_point"] is None for item in batch["items"])


def test_working_paper_per_batch_and_unknown_batch(client):
    register(client, "A", [("CTN-1", ["S-1"])])
    data = paper(client, "A")
    assert [item["serial_no"] for item in data["batches"][0]["items"]] == ["S-1"]
    assert client.get("/working-paper/NOPE").status_code == 404


def test_startup_backfills_working_paper_for_existing_batches(tmp_path):
    """A database created before the working paper existed gets seeded on boot."""
    settings = Settings(db_path=str(tmp_path / "legacy.db"), recalc_delay_seconds=0.0)
    with TestClient(create_app(settings)) as client:
        register(client, "A", [("CTN-1", ["S-1", "S-2"])])

    from app.db import Database

    db = Database(settings.db_path)
    with db.immediate() as conn:
        conn.execute("DELETE FROM working_paper_items")
        conn.execute("DELETE FROM working_paper_batches")

    with TestClient(create_app(settings)) as client:
        data = paper(client, "A")
        assert data["batches"][0]["mode"] == "whole_batch"
        assert [item["serial_no"] for item in data["batches"][0]["items"]] == [
            "S-1",
            "S-2",
        ]


# ---------------------------------------------------------------------------
# Happy path and queryability
# ---------------------------------------------------------------------------


def test_joint_settlement_replaces_passing_batches_with_received_items(client):
    register(client, "A", [("CTN-1", ["S-1", "S-2"])])
    register(client, "B", [("CTN-2", ["S-3", "S-4"])])
    scan(client, "A", "SC-A-1", "S-1", "P-A")
    scan(client, "B", "SC-B-1", "S-4", "P-B")

    body = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("B", active_result_id(client, "B")),
        ],
    ).json()
    assert body["settled_batch_ids"] == ["A", "B"]
    assert body["deferred_batch_ids"] == []

    a = batch_outcome(body, "A")
    assert a["status"] == "settled"
    assert a["previous_mode"] == "whole_batch"
    assert a["previous_item_count"] == 2
    assert a["working_paper_mode"] == "received"
    assert a["working_paper_result_id"] == active_result_id(client, "A")
    assert a["settled_item_count"] == 1
    assert a["released_serial_count"] == 1
    assert a["issues"] == []

    data = paper(client)
    by_batch = {item["batch_id"]: item for item in data["batches"]}
    assert by_batch["A"]["mode"] == "received"
    assert by_batch["A"]["result_id"] == active_result_id(client, "A")
    assert by_batch["A"]["result_is_current"] is True
    assert [item["serial_no"] for item in by_batch["A"]["items"]] == ["S-1"]
    received = by_batch["A"]["items"][0]
    assert received["receiving_point"] == "P-A"
    assert received["scan_ids"] == ["SC-A-1"]
    assert [item["serial_no"] for item in by_batch["B"]["items"]] == ["S-4"]
    assert data["serial_conflicts"] == []
    assert data["occupied_serial_count"] == 2


# ---------------------------------------------------------------------------
# Outdated result numbers
# ---------------------------------------------------------------------------


def test_outdated_result_number_defers_only_that_batch(client):
    register(client, "A", [("CTN-1", ["S-1"])])
    register(client, "B", [("CTN-2", ["S-2"])])
    scan(client, "A", "SC-A-1", "S-1")
    scan(client, "B", "SC-B-1", "S-2")

    stale = active_result_id(client, "B")
    scan(client, "B", "SC-B-2", "S-2")  # creates a newer current result
    current_b = active_result_id(client, "B")
    assert current_b != stale

    body = settle(
        client, [("A", active_result_id(client, "A")), ("B", stale)]
    ).json()
    assert body["settled_batch_ids"] == ["A"]
    assert body["deferred_batch_ids"] == ["B"]

    a = batch_outcome(body, "A")
    assert a["status"] == "settled"
    b = batch_outcome(body, "B")
    assert b["status"] == "deferred"
    assert b["submitted_result_id"] == stale
    assert b["submitted_result_is_current"] is False
    assert b["active_result_id"] == current_b
    assert [issue["code"] for issue in b["issues"]] == ["result_outdated"]
    # The deferred batch keeps its whole-batch draft, still occupying S-2.
    saved = paper(client, "B")["batches"][0]
    assert saved["mode"] == "whole_batch"
    assert [item["serial_no"] for item in saved["items"]] == ["S-2"]


def test_unknown_result_or_batch_is_404(client):
    register(client, "A", [("CTN-1", ["S-1"])])
    response = settle(client, [("A", 9999)])
    assert response.status_code == 404
    assert "9999" in response.text
    assert settle(client, [("NOPE", 1)]).status_code == 404


# ---------------------------------------------------------------------------
# Same serial claimed by several current results
# ---------------------------------------------------------------------------


def test_serial_claimed_by_two_current_results_defers_both(client):
    # Two batches declare the same physical serial and both report it received.
    register(client, "A", [("CTN-A", ["S-1", "S-A"])])
    register(client, "B", [("CTN-B", ["S-1", "S-B"])])
    scan(client, "A", "SC-A-1", "S-1", "P-A")
    scan(client, "B", "SC-B-1", "S-1", "P-B")

    body = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("B", active_result_id(client, "B")),
        ],
    ).json()
    assert body["settled_batch_ids"] == []
    assert body["deferred_batch_ids"] == ["A", "B"]
    for batch_id in ("A", "B"):
        outcome = batch_outcome(body, batch_id)
        codes = [issue["code"] for issue in outcome["issues"]]
        assert codes == ["serial_reported_by_multiple_results"]
        issue = outcome["issues"][0]
        assert issue["serial_numbers"] == ["S-1"]
        assert "S-1" in issue["message"]

    # Both drafts are untouched whole-batch drafts.
    saved = paper(client)
    assert saved["serial_conflicts"] == [{"serial_no": "S-1", "batch_ids": ["A", "B"]}]
    assert all(batch["mode"] == "whole_batch" for batch in saved["batches"])
    # A single-batch view reports the duplicate too, with the global owner.
    single = paper(client, "A")
    assert single["serial_conflicts"] == [
        {"serial_no": "S-1", "batch_ids": ["A", "B"]}
    ]
    assert single["batch_count"] == 1


def test_unsubmitted_batch_current_result_also_claiming_serial_defers(client):
    """The duplicate-claim rule looks at current results, not just submissions.

    B settled its draft with only S-B.  A later scan makes B's *current result*
    claim S-1 while its draft still does not occupy S-1, so only the
    duplicate-claim rule can defer A.
    """
    register(client, "A", [("CTN-A", ["S-1", "S-A"])])
    register(client, "B", [("CTN-B", ["S-1", "S-B"])])
    scan(client, "B", "SC-B-1", "S-B", "P-B")
    assert settle(client, [("B", active_result_id(client, "B"))]).json()[
        "settled_batch_ids"
    ] == ["B"]
    scan(client, "B", "SC-B-2", "S-1", "P-B")  # B's current result now claims S-1
    assert [item["serial_no"] for item in paper(client, "B")["batches"][0]["items"]] == [
        "S-B"
    ]

    scan(client, "A", "SC-A-1", "S-1", "P-A")
    body = settle(client, [("A", active_result_id(client, "A"))]).json()
    assert body["deferred_batch_ids"] == ["A"]
    issue = batch_outcome(body, "A")["issues"][0]
    assert issue["code"] == "serial_reported_by_multiple_results"
    assert issue["serial_numbers"] == ["S-1"]
    assert issue["related_batch_ids"] == ["B"]


# ---------------------------------------------------------------------------
# Draft retention and joint release
# ---------------------------------------------------------------------------


def test_unsubmitted_batch_draft_keeps_serial_and_defers_claimant(client):
    # B is never submitted: its draft keeps occupying S-1.
    register(client, "A", [("CTN-A", ["S-1", "S-A"])])
    register(client, "B", [("CTN-B", ["S-1", "S-B"])])
    scan(client, "A", "SC-A-1", "S-1")

    body = settle(client, [("A", active_result_id(client, "A"))]).json()
    assert body["deferred_batch_ids"] == ["A"]
    outcome = batch_outcome(body, "A")
    assert [issue["code"] for issue in outcome["issues"]] == [
        "serial_retained_by_unsettled_draft"
    ]
    assert outcome["issues"][0]["serial_numbers"] == ["S-1"]
    assert outcome["issues"][0]["related_batch_ids"] == ["B"]
    assert paper(client, "A")["batches"][0]["mode"] == "whole_batch"


def test_batches_releasing_the_same_serial_together_can_settle(client):
    # A received S-1; B declares S-1 but did not receive it, so settling B
    # releases the serial and lets both batches settle in one submission.
    register(client, "A", [("CTN-A", ["S-1", "S-A"])])
    register(client, "B", [("CTN-B", ["S-1", "S-B"])])
    scan(client, "A", "SC-A-1", "S-1")

    body = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("B", active_result_id(client, "B")),
        ],
    ).json()
    assert body["settled_batch_ids"] == ["A", "B"]
    assert all(batch_outcome(body, bid)["issues"] == [] for bid in ("A", "B"))

    saved = paper(client)
    assert saved["serial_conflicts"] == []
    by_batch = {batch["batch_id"]: batch for batch in saved["batches"]}
    assert [item["serial_no"] for item in by_batch["A"]["items"]] == ["S-1"]
    assert by_batch["B"]["items"] == []
    assert by_batch["B"]["mode"] == "received"


# ---------------------------------------------------------------------------
# Cascading deferral
# ---------------------------------------------------------------------------


def test_cascading_deferral_until_no_duplicate_remains(client):
    """A defers because of D's draft; A's retained draft then defers C.

    D is never submitted.  A and C are submitted and would both pass in
    isolation, but A collides with D on X, and once A is deferred its retained
    draft collides with C on Z.
    """
    register(client, "D", [("CTN-D", ["X"])])
    register(client, "A", [("CTN-A", ["X", "Z"])])
    register(client, "C", [("CTN-C", ["Z", "C-1"])])
    scan(client, "A", "SC-A-1", "X", "P-A")
    scan(client, "C", "SC-C-1", "Z", "P-C")

    body = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("C", active_result_id(client, "C")),
        ],
    ).json()
    assert body["settled_batch_ids"] == []
    assert body["deferred_batch_ids"] == ["A", "C"]

    a = batch_outcome(body, "A")
    assert [issue["code"] for issue in a["issues"]] == [
        "serial_retained_by_unsettled_draft"
    ]
    assert a["issues"][0]["serial_numbers"] == ["X"]
    assert a["issues"][0]["related_batch_ids"] == ["D"]

    c = batch_outcome(body, "C")
    assert [issue["code"] for issue in c["issues"]] == [
        "serial_retained_by_unsettled_draft"
    ]
    assert c["issues"][0]["serial_numbers"] == ["Z"]
    assert c["issues"][0]["related_batch_ids"] == ["A"]

    # All three drafts are unchanged; no serial was booked twice.
    saved = paper(client)
    assert all(batch["mode"] == "whole_batch" for batch in saved["batches"])
    assert saved["serial_conflicts"] == [
        {"serial_no": "X", "batch_ids": ["A", "D"]},
        {"serial_no": "Z", "batch_ids": ["A", "C"]},
    ]


def test_cascade_clears_when_occupying_batches_are_settled_together(client):
    """Submitting D as well releases X and Z, so A and C settle too."""
    register(client, "D", [("CTN-D", ["X"])])
    register(client, "A", [("CTN-A", ["X", "Z"])])
    register(client, "C", [("CTN-C", ["Z", "C-1"])])
    scan(client, "A", "SC-A-1", "X", "P-A")
    scan(client, "C", "SC-C-1", "Z", "P-C")

    body = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("C", active_result_id(client, "C")),
            ("D", active_result_id(client, "D")),
        ],
    ).json()
    assert body["settled_batch_ids"] == ["A", "C", "D"]
    assert body["deferred_batch_ids"] == []
    saved = paper(client)
    assert saved["serial_conflicts"] == []
    by_batch = {batch["batch_id"]: batch for batch in saved["batches"]}
    assert [item["serial_no"] for item in by_batch["A"]["items"]] == ["X"]
    assert [item["serial_no"] for item in by_batch["C"]["items"]] == ["Z"]
    assert by_batch["D"]["items"] == []


# ---------------------------------------------------------------------------
# Atomic write failure
# ---------------------------------------------------------------------------


def test_write_failure_rolls_back_every_passing_batch(client, monkeypatch):
    register(client, "A", [("CTN-A", ["S-A"])])
    register(client, "B", [("CTN-B", ["S-B"])])
    scan(client, "A", "SC-A-1", "S-A")
    scan(client, "B", "SC-B-1", "S-B")

    from app import service

    original = service._replace_working_paper
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated draft write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_replace_working_paper", flaky)
    response = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("B", active_result_id(client, "B")),
        ],
    )
    assert response.status_code == 500
    assert "保留原底稿" in response.text

    saved = paper(client)
    assert all(batch["mode"] == "whole_batch" for batch in saved["batches"])
    assert saved["item_count"] == 2

    # Retrying after the transient failure settles both batches.
    monkeypatch.undo()
    retry = settle(
        client,
        [
            ("A", active_result_id(client, "A")),
            ("B", active_result_id(client, "B")),
        ],
    ).json()
    assert retry["settled_batch_ids"] == ["A", "B"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_settlement_rejects_duplicate_or_empty_batch_list(client):
    register(client, "A", [("CTN-A", ["S-A"])])
    duplicate = client.post(
        "/settlements",
        json={
            "batches": [
                {"batch_id": "A", "result_id": 1},
                {"batch_id": "A", "result_id": 1},
            ]
        },
    )
    assert duplicate.status_code == 422
    assert client.post("/settlements", json={"batches": []}).status_code == 422


def test_settlement_uses_current_result_snapshot(client):
    """A later scan does not retroactively change what was settled."""
    register(client, "A", [("CTN-A", ["S-A", "S-B"])])
    scan(client, "A", "SC-A-1", "S-A")
    used = active_result_id(client, "A")
    body = settle(client, [("A", used)]).json()
    assert body["settled_batch_ids"] == ["A"]

    scan(client, "A", "SC-A-2", "S-B")
    saved = paper(client, "A")["batches"][0]
    assert saved["result_id"] == used
    assert saved["result_is_current"] is False
    assert [item["serial_no"] for item in saved["items"]] == ["S-A"]

    # Re-settling against the newer current result adds S-B and updates the
    # recorded result number.
    newer = active_result_id(client, "A")
    body = settle(client, [("A", newer)]).json()
    assert body["settled_batch_ids"] == ["A"]
    saved = paper(client, "A")["batches"][0]
    assert saved["result_id"] == newer
    assert [item["serial_no"] for item in saved["items"]] == ["S-A", "S-B"]
