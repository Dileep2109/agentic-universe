"""Store idempotency, separate KPIs, proposals, and supervisor authority."""

import json

import pytest

from controller.settings import DecisionThresholds
from controller.store import Store, WorldReset


def _lead(event_id: int, lead_id: str = "nw_renewal:l00000001") -> dict:
    return {
        "type": "lead",
        "ts": "2027-01-04T08:00:00+05:30",
        "workflow_id": "nw_renewal",
        "campaign_id": "nw_renewal:c0001",
        "lead_id": lead_id,
        "person_id": "nw_renewal:p0000001",
        "features": {"lang": "hi", "city_tier": "2", "age_band": "46-60", "value_band": "mid"},
        "event_id": event_id,
    }


def _call(event_id: int, *, connected: bool, engaged: bool, disposition: str, lead_id: str = "nw_renewal:l00000001") -> dict:
    return {
        "type": "call",
        "ts": "2027-01-04T09:15:00+05:30",
        "tenant_id": "northwind_life",
        "workflow_id": "nw_renewal",
        "campaign_id": "nw_renewal:c0001",
        "lead_id": lead_id,
        "person_id": "nw_renewal:p0000001",
        "attempt_no": 1,
        "campaign_day_index": 0,
        "config_version": 1,
        "cohort_id": "default",
        "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
        "connected": connected,
        "engaged": engaged,
        "disposition": disposition,
        "duration_s": 10,
        "turns": 2,
        "qa": {"hallucination_flag": False, "flow_adherence": 0.9} if connected else None,
        "event_id": event_id,
    }


def _events() -> list[dict]:
    return [
        {
            "type": "campaign_created",
            "ts": "2027-01-04T08:00:00+05:30",
            "tenant_id": "northwind_life",
            "workflow_id": "nw_renewal",
            "campaign_id": "nw_renewal:c0001",
            "leads": 2,
            "config_version_at_creation": 1,
            "event_id": 1,
        },
        _lead(2, "nw_renewal:l00000001"),
        _lead(3, "nw_renewal:l00000002"),
        _call(4, connected=True, engaged=True, disposition="concluded_interested"),
        _call(5, connected=True, engaged=False, disposition="short_hangup", lead_id="nw_renewal:l00000002"),
        _call(6, connected=False, engaged=False, disposition="no_answer", lead_id="nw_renewal:l00000002"),
        _lead(7, "nw_renewal:l00000003"),
    ]


def test_chunk_replay_does_not_duplicate(tmp_path):
    store = Store.open(tmp_path, "fake")
    meta = {"chunk": 0, "kind": "history", "until_ts": "2027-01-04T09:15:00+05:30"}
    store.apply_chunk(meta, _events(), "history")
    store.apply_chunk(meta, _events(), "history")
    totals = store.call_totals()
    assert totals == {"dials": 3, "connected": 2, "engaged": 1}
    assert store.lead_count() == 3
    assert store.last_event_id == 7
    assert store.ingest_counts()[("history", "call")] == 3
    assert store.ingest_counts()[("history", "lead")] == 3


def test_kpis_stay_separate(tmp_path):
    store = Store.open(tmp_path, "fake")
    store.apply_chunk({"chunk": 0}, _events(), "history")
    kpi = store.kpis()["nw_renewal"]
    assert kpi["pickup_rate"] == pytest.approx(2 / 3)
    assert kpi["engagement_rate"] == pytest.approx(1 / 2)
    assert kpi["business_outcome"] == pytest.approx(1 / 3)
    assert len({kpi["pickup_rate"], kpi["engagement_rate"], kpi["business_outcome"]}) == 3
    assert "blended" not in kpi
    assert set(kpi) == {
        "dials",
        "connected",
        "engaged",
        "leads_uploaded",
        "leads_concluded",
        "pickup_rate",
        "engagement_rate",
        "business_outcome",
    }


def test_proposal_stores_exact_config_evidence_and_outcome(tmp_path):
    store = Store.open(tmp_path, "fake")
    config = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 1}}}, "prompt_version": "P1", "schedule": {}}
    evidence = {"validation_run_id": "val_00001", "pass_rate": 0.99}
    calculation = {"method": "relative_primary_lift", "lift": 0.04, "n": 400, "clipped": 0.2667}
    store.save_proposal({
        "proposal_id": "prop-1",
        "workflow_id": "nw_renewal",
        "base_version": 4,
        "proposed_config": config,
        "change_summary": "shift voice",
        "diff": {"voice": {"V1": 1}},
        "affected_leads": 12,
        "dry_run": {"n": 400, "method": "historical_reweight"},
        "impact_score": 0.27,
        "impact_calculation": calculation,
        "rationale": "enough evidence",
        "evidence": evidence,
        "approval_id": "apr_00001",
        "status": "pending",
        "outcome": None,
    })
    store.apply_chunk({"chunk": 0}, [{
        "type": "approval_decided",
        "ts": "2027-01-04T11:00:00+05:30",
        "approval_id": "apr_00001",
        "workflow_id": "nw_renewal",
        "status": "approved",
        "reasons": [],
        "event_id": 1,
    }], "events")
    saved = store.get_proposal("prop-1")
    assert saved is not None
    assert saved["proposed_config"] == config
    assert saved["evidence"] == evidence
    assert saved["impact_calculation"] == calculation
    assert saved["status"] == "approved"
    assert saved["impact_score"] == pytest.approx(0.27)


def test_supervisor_change_is_authoritative_and_not_reversed(tmp_path):
    store = Store.open(tmp_path, "fake")
    store.apply_chunk({"chunk": 0}, [{
        "type": "config_changed",
        "ts": "2027-01-04T15:00:00+05:30",
        "workflow_id": "nw_renewal",
        "version": 2,
        "actor": "supervisor",
        "changed_dims": ["voice"],
        "proposal_id": None,
        "approval_id": None,
        "event_id": 1,
    }], "events")
    changes = store.authoritative_changes()
    assert len(changes) == 1
    assert changes[0]["actor"] == "supervisor"
    assert store.get_proposal("anything") is None
    assert store.audit_actions() == ["supervisor_change_authoritative"]
    detail = json.loads(store.conn.execute("SELECT detail_json FROM audit_log").fetchone()["detail_json"])
    assert "will not reverse" in detail["note"]


def test_world_reset_when_server_is_behind(tmp_path):
    store = Store.open(tmp_path, "fake")
    store.apply_chunk({"chunk": 0}, [_lead(1)], "history")
    with pytest.raises(WorldReset):
        store.ensure_same_world({
            "scenario": "fake",
            "sim_minute": 0,
            "last_event_id": 0,
        })


def test_pending_advance_roundtrip(tmp_path):
    store = Store.open(tmp_path, "fake")
    assert store.pending_advance_from_minute is None
    store.mark_pending_advance(15)
    assert store.pending_advance_from_minute == 15
    store.adopt_clock({"sim_minute": 30, "ts": "2027-01-04T00:30:00+05:30"})
    assert store.pending_advance_from_minute is None
    assert store.sim_minute == 30


def test_thresholds_are_configurable_and_unused_by_phase1_loop():
    defaults = DecisionThresholds()
    tuned = defaults.overridden(engagement_min_connected=800, alpha=0.01)
    assert tuned.engagement_min_connected == 800
    assert tuned.alpha == 0.01
    assert defaults.engagement_min_connected == 400
    with pytest.raises(ValueError):
        defaults.overridden(not_a_threshold=1)
