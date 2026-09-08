from datetime import date

import pytest

from app.models import RepairTicketItem, SnAsset
from app.services.manual_review import _apply_manual_sn_master_selections
from app.services.sn_master_resolution import asset_snapshot


class Rows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class Session:
    def __init__(self, item, asset, candidates):
        self.item = item
        self.asset = asset
        self.candidates = candidates
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return Rows([self.item] if self.calls == 1 else self.candidates)

    async def get(self, model, identity):
        return self.asset if model is SnAsset and identity == self.asset.id else None


@pytest.mark.anyio
async def test_manual_selection_must_come_from_ambiguous_candidate_set() -> None:
    asset = SnAsset(
        id=22,
        ins_id=1022,
        sn="SN0001",
        customer_code="CM1",
        customer_name="Customer",
        material_code="Z.SM.XA",
        material_name="A board",
        warranty_end_date=date(2026, 1, 1),
        asset_status="valid",
        source_system="sqlserver",
    )
    other = SnAsset(
        id=23,
        ins_id=1023,
        sn="SN0001",
        customer_code="CM1",
        customer_name="Customer",
        material_code="Z.SM.YA",
        material_name="Other A board",
        warranty_end_date=date(2026, 1, 1),
        asset_status="valid",
        source_system="sqlserver",
    )
    item = RepairTicketItem(
        id=2,
        ticket_id=1,
        line_no=1,
        sn="SN0001",
        sn_master_resolution_status="MASTER_DATA_AMBIGUOUS",
        sn_master_resolution_snapshot={
            "status": "MASTER_DATA_AMBIGUOUS",
            "candidate_ids": [22, 23],
            "candidates": [asset_snapshot(asset)],
        },
    )

    await _apply_manual_sn_master_selections(
        Session(item, asset, [asset, other]),
        ticket_id=1,
        result_payload={
            "sn_master_selections": [{"ticket_item_id": 2, "sn_asset_id": 22}]
        },
    )

    assert item.sn_asset_id == 22
    assert item.sn_master_resolution_status == "RESOLVED"
    assert item.sn_master_resolution_method == "MANUAL"
    assert item.sn_master_resolution_snapshot["resolved_asset"]["ins_id"] == 1022
