from __future__ import annotations

import pytest

from app.models import BatchIn, CorrectionIn, JointSettlementIn, ScanIn


def test_scan_accepts_carton_with_intact_seal():
    scan = ScanIn(
        scan_id="SC-1",
        receiving_point="A",
        carton_no="CTN-1",
        seal_intact=True,
    )
    assert scan.kind == "carton"


@pytest.mark.parametrize(
    "payload",
    [
        {"scan_id": "S", "receiving_point": "A", "carton_no": "C"},  # no seal
        {"scan_id": "S", "receiving_point": "A", "carton_no": "C", "seal_intact": False},
        {
            "scan_id": "S",
            "receiving_point": "A",
            "carton_no": "C",
            "serial_no": "X",
            "seal_intact": True,
        },
        {"scan_id": "S", "receiving_point": "A"},
        {"scan_id": "S", "receiving_point": "A", "serial_no": "X", "seal_intact": True},
        {"scan_id": "S", "receiving_point": "A", "serial_no": " "},
        {"scan_id": "S", "receiving_point": "A", "serial_no": "X", "extra": 1},
    ],
)
def test_scan_rejects_invalid_shapes(payload):
    with pytest.raises(ValueError):
        ScanIn(**payload)


def test_batch_rejects_empty_carton_and_duplicate_serials():
    with pytest.raises(ValueError):
        BatchIn(batch_id="B", cartons=[{"carton_no": "C", "serials": []}])
    with pytest.raises(ValueError):
        BatchIn(
            batch_id="B",
            cartons=[{"carton_no": "C", "serials": ["S", "S"]}],
        )
    with pytest.raises(ValueError):
        BatchIn(
            batch_id="B",
            cartons=[
                {"carton_no": "C1", "serials": ["S"]},
                {"carton_no": "C2", "serials": ["S"]},
            ],
        )
    with pytest.raises(ValueError):
        BatchIn(
            batch_id="B",
            cartons=[
                {"carton_no": "C1", "serials": ["S1"]},
                {"carton_no": "C1", "serials": ["S2"]},
            ],
        )


def test_correction_rejects_duplicate_moves():
    with pytest.raises(ValueError):
        CorrectionIn(
            moves=[
                {"serial_no": "S", "carton_no": "C1"},
                {"serial_no": "S", "carton_no": "C2"},
            ]
        )


def test_joint_settlement_rejects_duplicate_batches_and_bad_result_ids():
    with pytest.raises(ValueError):
        JointSettlementIn(
            batches=[
                {"batch_id": "B", "result_id": 1},
                {"batch_id": "B", "result_id": 2},
            ]
        )
    with pytest.raises(ValueError):
        JointSettlementIn(batches=[])
    with pytest.raises(ValueError):
        JointSettlementIn(batches=[{"batch_id": "B", "result_id": 0}])
    with pytest.raises(ValueError):
        JointSettlementIn(batches=[{"batch_id": "B", "result_id": 1, "extra": 1}])


def test_joint_settlement_accepts_multiple_batches():
    payload = JointSettlementIn(
        batches=[
            {"batch_id": "B-1", "result_id": 1},
            {"batch_id": " B-2 ", "result_id": 7},
        ]
    )
    assert [entry.batch_id for entry in payload.batches] == ["B-1", "B-2"]
    assert [entry.result_id for entry in payload.batches] == [1, 7]
