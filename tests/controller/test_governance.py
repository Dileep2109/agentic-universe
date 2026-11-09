"""Policy, proposal, approval, auto-apply, and rollback behaviour.

Unit tests use an in-memory client. One test talks to the real public_01
simulator and checks the workflow documents it returns.
"""

from __future__ import annotations

import copy
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from controller.client import AmbiguousResponse, ReconciledWrite, SimClient, SimError
from controller.governance import build_proposal, detect_opportunities, govern, proposal_id_for, verify_configuration
from controller.impact import assess_impact
from controller.policy import evaluate_policy, total_variation
from controller.settings import RunSettings
from controller.store import Store

ROOT = Path(__file__).resolve().parents[2]


def _workflow(
    *,
    auto=True,
    dims=("caller_id", "schedule"),
    shift=0.5,
    hours=(10, 19),
    limit=3,
    ttl=24,
    mid="allowed",
    active=None,
):
    document = {
        "workflow_id": "nw_renewal",
        "mid_flight": mid,
        "dialling_window": [9, 21],
        "options": {
            "welcome_message": ["W1", "W2"],
            "voice": ["V1", "V2"],
            "caller_id": ["D1", "D2"],
            "prompt_version": ["P1", "P2"],
        },
        "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
        "governance": {
            "auto_apply": {"enabled": auto, "allowed_dims": list(dims), "max_shift": shift},
            "approver_hours": list(hours),
            "approver_latency_minutes": [30, 240],
            "proposal_ttl_hours": ttl,
            "apply_window_hours": 2,
            "max_requests_per_24h": limit,
        },
    }
    if active is not None:
        document["active"] = active
    return document


def _current(weights=None, version=1, actor="business_default"):
    weights = weights or {"D1": 0.5, "D2": 0.5}
    return {
        "workflow_id": "nw_renewal",
        "version": version,
        "actor": actor,
        "config": {
            "cohorts": [],
            "allocations": {
                "default": {
                    "welcome_message": {"W1": 1.0},
                    "voice": {"V1": 1.0},
                    "caller_id": dict(weights),
                }
            },
            "prompt_version": "P1",
            "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
        },
    }


def _moved(current, source="D1", dest="D2", delta=0.2):
    proposed = copy.deepcopy(current["config"])
    weights = proposed["allocations"]["default"]["caller_id"]
    weights[source] -= delta
    weights[dest] += delta
    return proposed


def _evidence(version=1, explained=True):
    return {
        "explained": explained,
        "config_version": version,
        "config_snapshot": _current()["config"],
        "affected_population": 4000,
        "evidence_ids": ["caller:nw_renewal:1:D1:D2"],
        "causal": False,
        "effect_kind": "observed",
    }


def _impact():
    return assess_impact(
        primary_kpi="pickup_rate",
        guardrail_kpi="engagement_rate",
        baseline_rate=0.2,
        observed_rate=0.4,
        sample_n=4000,
        ci_low=0.18,
        ci_high=0.22,
        affected_population=4000,
    )


def _decide(proposed, **policy):
    current = policy.pop("current", _current())
    return evaluate_policy(
        policy.pop("workflow", _workflow()),
        current,
        proposed,
        sim_ts=policy.pop("sim_ts", "2027-01-04T11:00:00+05:30"),
        pending_approvals=policy.pop("pending", 0),
        recent_requests=policy.pop("recent", 0),
        evidence_version=policy.pop("evidence_version", current["version"]),
        validation_run_id=policy.pop("validation_run_id", None),
        proposal_age_hours=policy.pop("age", None),
    )


def test_auto_apply_allows_an_in_policy_caller_shift():
    proposed = _moved(_current(), delta=0.2)
    decision = _decide(proposed)
    assert decision["decision"] == "auto_apply_eligible"
    assert decision["ready_to_submit"] is True
    assert decision["changed_dims"] == ["caller_id"]
    assert decision["current_version"] == 1
    assert decision["expected_version_after_apply"] == 2
    assert abs(decision["shift"] - 0.2) < 1e-9


def test_shift_equal_to_the_maximum_is_allowed_and_above_it_is_not_auto():
    at_limit = _decide(_moved(_current(), delta=0.5))
    assert at_limit["decision"] == "auto_apply_eligible"
    assert abs(at_limit["shift"] - 0.5) < 1e-9
    heavy = _current(weights={"D1": 0.8, "D2": 0.2})
    over = _decide(_moved(heavy, delta=0.51), current=heavy)
    assert over["decision"] == "approval_required"
    assert over["route"] == "approval"


def test_disallowed_dimension_and_value_and_disabled_auto_apply():
    proposed = _moved(_current())
    welcome = copy.deepcopy(_current()["config"])
    welcome["allocations"]["default"]["welcome_message"] = {"W2": 1.0}
    decision = _decide(welcome)
    assert decision["decision"] == "approval_required"
    assert "welcome_message" in decision["changed_dims"]
    blocked = _decide(proposed, workflow=_workflow(auto=False, dims=()))
    assert blocked["decision"] == "approval_required"
    assert blocked["route"] == "approval"
    bad = _moved(_current())
    bad["allocations"]["default"]["caller_id"]["D9"] = bad["allocations"]["default"]["caller_id"].pop("D2")
    rejected = _decide(bad)
    assert rejected["decision"] == "rejected"
    assert "value_not_allowed" in rejected["reason_codes"]


def test_time_window_rate_limit_ttl_and_apply_window_are_read_from_the_document():
    proposed = _moved(_current())
    early = _decide(proposed, workflow=_workflow(auto=False), sim_ts="2027-01-04T09:30:00+05:30")
    assert early["decision"] == "approval_required"
    assert early["ready_to_submit"] is False
    assert early["reason_codes"] == ["outside_approver_hours"]
    assert early["apply_window_hours"] == 2
    open_hours = _decide(proposed, workflow=_workflow(auto=False), sim_ts="2027-01-04T18:30:00+05:30")
    assert open_hours["ready_to_submit"] is True
    limited = _decide(proposed, recent=3)
    assert limited["decision"] == "rejected"
    assert "rate_limit" in limited["reason_codes"]
    expired = _decide(proposed, age=25)
    assert expired["decision"] == "rejected"
    assert "proposal_expired" in expired["reason_codes"]
    auto_early = _decide(proposed, sim_ts="2027-01-04T09:30:00+05:30")
    assert auto_early["decision"] == "auto_apply_eligible"
    assert auto_early["ready_to_submit"] is True


def test_stale_config_supervisor_edit_pending_and_missing_policy_fail_closed():
    proposed = _moved(_current())
    stale = _decide(proposed, evidence_version=0)
    assert "stale_configuration" in stale["reason_codes"]
    supervised = _current(actor="supervisor")
    supervised["supervisor_after_evidence"] = True
    held = _decide(proposed, current=supervised)
    assert "supervisor_edit" in held["reason_codes"]
    pending = _decide(proposed, pending=1)
    assert "pending_approval" in pending["reason_codes"]
    missing = evaluate_policy(
        {"workflow_id": "nw_renewal"},
        _current(),
        proposed,
        sim_ts="2027-01-04T11:00:00+05:30",
        pending_approvals=0,
        recent_requests=0,
        evidence_version=1,
    )
    assert missing["decision"] == "rejected"
    assert "missing_policy" in missing["reason_codes"]
    inactive = _decide(proposed, workflow=_workflow(active=False))
    assert "workflow_inactive" in inactive["reason_codes"]
    locked = _decide(proposed, workflow=_workflow(mid="next_campaign_only"))
    assert "locked_until_next_campaign" in locked["reason_codes"]


def test_prompt_change_without_validation_is_rejected():
    proposed = copy.deepcopy(_current()["config"])
    proposed["prompt_version"] = "P2"
    rejected = _decide(proposed)
    assert "prompt_requires_validation" in rejected["reason_codes"]
    allowed = _decide(proposed, validation_run_id="val_1", workflow=_workflow(auto=False))
    assert allowed["decision"] == "approval_required"


def test_impact_does_not_invent_an_uplift():
    known = _impact()
    assert known["effect_kind"] == "observed"
    assert known["expected_effect"] is None
    assert known["assumed_effect"] is None
    assert known["observed_effect"] == 0.2
    assert 0.0 <= known["impact_score"] <= 1.0
    unknown = assess_impact(
        primary_kpi=None,
        guardrail_kpi=None,
        baseline_rate=None,
        observed_rate=None,
        sample_n=0,
        ci_low=None,
        ci_high=None,
        affected_population=None,
    )
    assert unknown["effect_kind"] == "unverified"
    assert unknown["impact_score"] == 0.0


def test_proposal_requires_explained_evidence_and_is_deterministic():
    workflow = _workflow()
    current = _current()
    proposed = _moved(current)
    weak = build_proposal(
        workflow, current, proposed, _evidence(explained=False), _impact(),
        sim_ts="2027-01-04T11:00:00+05:30", pending_approvals=0, recent_requests=0,
        rationale="not explained",
    )
    assert weak["proposal"] is None
    assert "unexplained_change" in weak["policy"]["reason_codes"]
    first = build_proposal(
        workflow, current, proposed, _evidence(), _impact(),
        sim_ts="2027-01-04T11:00:00+05:30", pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )
    second = build_proposal(
        workflow, current, proposed, _evidence(), _impact(),
        sim_ts="2027-01-04T11:00:00+05:30", pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )
    assert first["proposal"]["proposal_id"] == second["proposal"]["proposal_id"]
    assert first["proposal"]["proposal_id"] == proposal_id_for("nw_renewal", 1, proposed)
    assert first["proposal"]["impact_calculation"]["expected_effect"] is None
    assert first["proposal"]["policy"]["decision"] == "auto_apply_eligible"
    invented = _impact()
    invented["expected_effect"] = 0.05
    refused = build_proposal(
        workflow, current, proposed, _evidence(), invented,
        sim_ts="2027-01-04T11:00:00+05:30", pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )
    assert refused["proposal"] is None
    assert "impact_not_explainable" in refused["policy"]["reason_codes"]


def _seed_calls(
    store: Store,
    caller: str,
    dials: int,
    connected: int,
    engaged: int,
    version: int = 1,
    *,
    call_date: str = "2027-01-04",
    hour: int = 11,
    no_answer: int = 0,
    voicemail: int = 0,
):
    store.conn.execute(
        """INSERT INTO call_agg (
               workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
               campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
               voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
               concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
           ) VALUES (?, 'events', 'c', ?, 'default', ?, ?, 0, 'hi', '1', '18-30', 'low',
                     'W1', 'V1', ?, 'P1', ?, ?, ?, 0, 0, 0, ?, ?, 0)""",
        ("nw_renewal", version, call_date, hour, caller, dials, connected, engaged, no_answer, voicemail),
    )
    store._rebuild_group_totals()


def test_detector_needs_a_confirmed_gap_and_refuses_a_worse_engagement_destination(tmp_path):
    store = Store.open(tmp_path, "unit")
    try:
        _seed_calls(store, "D1", 2000, 400, 200)
        _seed_calls(store, "D2", 2000, 800, 400)
        found = detect_opportunities(store, _workflow(), _current(), RunSettings())
        assert len(found) == 1
        assert found[0]["actionable"] is True
        assert found[0]["evidence"]["causal"] is False
        assert found[0]["proposed_config"]["allocations"]["default"]["caller_id"].get("D1", 0) < 0.5
        shift = total_variation(_current()["config"], found[0]["proposed_config"])
        assert shift <= 0.5 + 1e-9

        store.conn.execute("DELETE FROM call_agg")
        store.conn.execute("DELETE FROM call_group_totals")
        _seed_calls(store, "D1", 100, 40, 20)
        _seed_calls(store, "D2", 100, 80, 40)
        assert detect_opportunities(store, _workflow(), _current(), RunSettings()) == []

        store.conn.execute("DELETE FROM call_agg")
        store.conn.execute("DELETE FROM call_group_totals")
        _seed_calls(store, "D1", 2000, 400, 360)
        _seed_calls(store, "D2", 2000, 800, 400)
        blocked = detect_opportunities(store, _workflow(), _current(), RunSettings())
        assert blocked[0]["actionable"] is False
        assert "engagement" in blocked[0]["reason"]
    finally:
        store.close()


class FakeWorld:
    def __init__(self, workflow=None, current=None):
        self.workflow = workflow or _workflow()
        self.current = current or _current()
        self.approvals = []
        self.history = [{
            "version": self.current["version"],
            "actor": self.current["actor"],
            "proposal_id": None,
            "approval_id": None,
            "changed_dims": [],
        }]
        self.ts = "2027-01-04T11:00:00+05:30"
        self.fail = None
        self.approval_status = "pending"
        self.writes = []
        self.verify_version = None

    def state(self):
        return {"ts": self.ts, "sim_minute": 660, "credits_remaining": 40}

    def workflows(self):
        return [self.workflow]

    def get_config(self, workflow_id):
        if self.verify_version is not None:
            return {**self.current, "version": self.verify_version}
        return self.current

    def list_approvals(self, workflow_id=None):
        return list(self.approvals)

    def find_approval_by_proposal(self, proposal_id, workflow_id=None):
        for row in self.approvals:
            if row["proposal_id"] == proposal_id:
                return row
        return None

    def find_config_by_proposal(self, workflow_id, proposal_id):
        for row in self.history:
            if row.get("proposal_id") == proposal_id:
                return row
        return None

    def find_config_by_approval(self, workflow_id, approval_id):
        for row in self.history:
            if row.get("approval_id") == approval_id:
                return row
        return None

    def config_history(self, workflow_id):
        return list(self.history)

    def request_approval(self, body):
        self.writes.append("approval")
        if self.fail == "timeout":
            raise AmbiguousResponse(0, "transport", "lost")
        if self.fail == "duplicate":
            raise SimError(409, "duplicate_proposal_id", body["proposal_id"])
        if self.fail == "conflict":
            raise SimError(409, "stale_base_version", {"current_version": 2})
        if self.fail == "rate":
            raise SimError(429, "rate_limit", "slow down")
        approval = {
            "approval_id": "apr_1",
            "proposal_id": body["proposal_id"],
            "workflow_id": body["workflow_id"],
            "status": self.approval_status,
            "base_version": body["base_version"],
            "changed_dims": ["caller_id"],
            "created_at": self.ts,
            "expires_at": "2027-01-05T11:00:00+05:30",
            "_config": body["proposed_config"],
        }
        self.approvals.append(approval)
        return ReconciledWrite(True, False, approval, "created")

    def auto_apply(self, workflow_id, body):
        self.writes.append("auto")
        if self.fail == "timeout":
            raise AmbiguousResponse(0, "transport", "lost")
        if self.fail == "forbidden":
            raise SimError(403, "auto_apply_not_permitted", ["not allowed"])
        self.current = {
            **self.current,
            "version": self.current["version"] + 1,
            "actor": "controller_auto",
            "config": body["proposed_config"],
        }
        self.history.append({
            "version": self.current["version"],
            "actor": "controller_auto",
            "proposal_id": body["proposal_id"],
            "approval_id": None,
            "changed_dims": ["caller_id"],
        })
        return ReconciledWrite(
            True, False,
            {"workflow_id": workflow_id, "version": self.current["version"], "changed_dims": ["caller_id"]},
            "applied",
        )

    def apply_approval(self, workflow_id, approval_id):
        self.writes.append("apply")
        if self.fail == "timeout":
            raise AmbiguousResponse(0, "transport", "lost")
        if self.fail == "lapsed":
            raise SimError(410, "approval_lapsed", approval_id)
        approval = self.approvals[0]
        self.current = {
            **self.current,
            "version": self.current["version"] + 1,
            "actor": "controller_approved",
            "config": approval.get("_config", self.current["config"]),
        }
        self.history.append({
            "version": self.current["version"],
            "actor": "controller_approved",
            "proposal_id": approval["proposal_id"],
            "approval_id": approval_id,
            "changed_dims": ["caller_id"],
        })
        approval["status"] = "applied"
        return ReconciledWrite(
            True, False,
            {"workflow_id": workflow_id, "version": self.current["version"], "changed_dims": ["caller_id"]},
            "applied",
        )


def _prepare(tmp_path, engagement_dest=400):
    path = tmp_path / f"case-{engagement_dest}-{len(list(tmp_path.iterdir())) if tmp_path.exists() else 0}"
    store = Store.open(path, "unit")
    _seed_calls(store, "D1", 2000, 400, 200)
    _seed_calls(store, "D2", 2000, 800, engagement_dest)
    return store


def test_auto_apply_verifies_the_resulting_version(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld()
    try:
        summary = govern(store, world, RunSettings())
        assert summary["auto_applied"] == 1
        assert summary["verified"] == 1
        assert world.writes == ["auto"]
        saved = store.proposals_by_status(("verified",))
        assert len(saved) == 1
        assert saved[0]["verification"]["ok"] is True
        assert saved[0]["verification"]["version"] == 2
        again = govern(store, world, RunSettings())
        assert again["auto_applied"] == 0
        assert world.writes == ["auto"]
        assert "configuration_applied" in store.audit_actions()
    finally:
        store.close()


def test_auto_apply_rejects_a_forbidden_change_and_a_version_mismatch(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld()
    world.fail = "forbidden"
    try:
        govern(store, world, RunSettings())
        assert world.writes == ["auto"]
        failed = store.proposals_by_status(("failed",))
        assert failed[0]["outcome"] == "auto_apply_not_permitted"
        govern(store, world, RunSettings())
        assert world.writes == ["auto"]
    finally:
        store.close()

    store = _prepare(tmp_path)
    world = FakeWorld()
    world.verify_version = 1
    try:
        govern(store, world, RunSettings())
        row = store.proposals_by_status(("uncertain",))[0]
        assert row["outcome"] == "verification_mismatch"
        assert "configuration_verification_failed" in store.audit_actions()
    finally:
        store.close()


def test_timeout_before_creation_retries_once_and_timeout_after_does_not(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    world.fail = "timeout"
    try:
        govern(store, world, RunSettings())
        assert store.proposals_by_status(("uncertain",))
        world.fail = None
        govern(store, world, RunSettings())
        assert world.writes.count("approval") == 2
        assert store.proposals_by_status(("pending",))
    finally:
        store.close()

    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))

    def timeout_but_created(body):
        world.writes.append("approval")
        approval = {
            "approval_id": "apr_1",
            "proposal_id": body["proposal_id"],
            "workflow_id": body["workflow_id"],
            "status": "pending",
            "created_at": world.ts,
            "expires_at": "2027-01-05T11:00:00+05:30",
        }
        world.approvals.append(approval)
        raise AmbiguousResponse(0, "transport", "lost after create")

    world.request_approval = timeout_but_created
    try:
        govern(store, world, RunSettings())
        assert store.proposals_by_status(("uncertain",))
        govern(store, world, RunSettings())
        assert world.writes == ["approval"]
        assert store.proposals_by_status(("pending",))
    finally:
        store.close()


def test_conflict_rate_limit_and_approved_apply(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    world.fail = "conflict"
    try:
        govern(store, world, RunSettings())
        assert store.proposals_by_status(("failed",))[0]["outcome"] == "stale_base_version"
    finally:
        store.close()

    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    world.fail = "rate"
    try:
        govern(store, world, RunSettings())
        assert world.writes == ["approval"]
        drafted = store.proposals_by_status(("drafted",))[0]
        assert drafted["outcome"] in ("rate_limited", "retry_after_rate_limit")
    finally:
        store.close()

    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    try:
        govern(store, world, RunSettings())
        assert store.proposals_by_status(("pending",))
        world.approvals[0]["status"] = "approved"
        govern(store, world, RunSettings())
        assert world.writes == ["approval", "apply"]
        saved = store.proposals_by_status(("verified",))
        assert saved[0]["verification"]["version"] == 2
        assert saved[0]["approval_id"] == "apr_1"
    finally:
        store.close()


def test_duplicate_proposal_is_reconciled_instead_of_sent_again(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    try:
        first = govern(store, world, RunSettings())
        assert first["submitted"] == 1
        proposal = store.proposals_by_status(("pending",))[0]
        proposal["status"] = "uncertain"
        proposal["outcome"] = "response_lost"
        store.save_proposal(proposal)
        govern(store, world, RunSettings())
        assert world.writes == ["approval"]
        assert store.proposals_by_status(("pending",))
    finally:
        store.close()


def test_expired_approval_is_not_applied(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    try:
        govern(store, world, RunSettings())
        world.approvals[0]["status"] = "expired"
        govern(store, world, RunSettings())
        assert "apply" not in world.writes
        assert store.proposals_by_status(("expired",))
        assert "approval_expired" in store.audit_actions()
    finally:
        store.close()


def test_lapsed_apply_is_not_retried(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    world.fail = "lapsed"
    try:
        govern(store, world, RunSettings())
        world.approvals[0]["status"] = "approved"
        world.fail = "lapsed"
        govern(store, world, RunSettings())
        assert world.writes.count("apply") == 1
        assert store.proposals_by_status(("lapsed",))
        govern(store, world, RunSettings())
        assert world.writes.count("apply") == 1
    finally:
        store.close()


def test_rollback_distinguishes_noise_small_samples_and_supervisor_edits(tmp_path):
    from controller.governance import _monitor_applied

    def verified(store, world, post_connected, post_dials=2000, actor="controller_auto", version=2):
        proposal = build_proposal(
            world.workflow, _current(), _moved(_current(), delta=0.2), _evidence(), _impact(),
            sim_ts=world.ts, pending_approvals=0, recent_requests=0,
            rationale="Observed pickup differs. No uplift is claimed.",
        )["proposal"]
        proposal["status"] = "verified"
        proposal["evidence"]["baseline_n"] = 2000
        proposal["evidence"]["baseline_successes"] = 800
        proposal["evidence"]["config_snapshot"] = _current()["config"]
        proposal["verification"] = {"ok": True, "version": 2}
        store.save_proposal(proposal)
        _seed_calls(store, "D1", post_dials, post_connected, max(post_connected // 2, 0), version=version)
        world.current = _current(version=version, actor=actor)
        world.current["config"] = _moved(_current(), delta=0.2)

    store = Store.open(tmp_path / "drop", "unit")
    world = FakeWorld()
    try:
        verified(store, world, post_connected=200)
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "degradation_detected" in store.audit_actions()
        assert "rollback_recommended" in store.audit_actions()
        assert world.writes == []
    finally:
        store.close()

    store = Store.open(tmp_path / "noise", "unit")
    world = FakeWorld()
    try:
        verified(store, world, post_connected=790)
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "degradation_detected" not in store.audit_actions()
    finally:
        store.close()

    store = Store.open(tmp_path / "small", "unit")
    world = FakeWorld()
    try:
        verified(store, world, post_connected=20, post_dials=50)
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "degradation_detected" not in store.audit_actions()
    finally:
        store.close()

    store = Store.open(tmp_path / "supervisor", "unit")
    world = FakeWorld()
    try:
        verified(store, world, post_connected=200, actor="supervisor", version=3)
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "supervisor_modification_detected" in store.audit_actions()
        assert "rollback_recommended" not in store.audit_actions()
        assert world.writes == []
    finally:
        store.close()


def test_restart_does_not_submit_the_same_approval_twice(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    try:
        govern(store, world, RunSettings())
        state_dir = store.path.parent
        store.close()
        reopened = Store.open(state_dir, "unit")
        try:
            govern(reopened, world, RunSettings())
            assert world.writes == ["approval"]
            assert reopened.proposals_by_status(("pending",))
        finally:
            reopened.close()
    except Exception:
        try:
            store.close()
        except Exception:
            pass
        raise


def test_confirmed_degradation_rolls_back_through_policy_and_verifies(tmp_path):
    store = Store.open(tmp_path, "unit")
    world = FakeWorld()
    proposal = build_proposal(
        world.workflow, _current(), _moved(_current(), delta=0.2), _evidence(), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "verified"
    proposal["evidence"]["baseline_n"] = 2000
    proposal["evidence"]["baseline_successes"] = 800
    proposal["evidence"]["config_snapshot"] = _current()["config"]
    proposal["verification"] = {"ok": True, "version": 2}
    store.save_proposal(proposal)
    _seed_calls(store, "D1", 2000, 200, 100, version=2)
    world.current = _current(version=2, actor="controller_auto")
    world.current["config"] = _moved(_current(), delta=0.2)
    try:
        summary = govern(store, world, RunSettings())
        assert "auto" in world.writes
        assert summary["applied"] >= 1
        rolled = [
            row for row in store.proposals_by_status(("verified",))
            if row["evidence"].get("rollback_of")
        ]
        assert len(rolled) == 1
        assert rolled[0]["verification"]["ok"] is True
        assert "rollback_applied" in store.audit_actions()
        assert "degradation_detected" in store.audit_actions()
    finally:
        store.close()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_public_01_policy_matches_the_simulator_and_a_legal_auto_apply_verifies(tmp_path):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "sim.log"
    log_file = log_path.open("w", encoding="utf-8", buffering=1)
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "sim", "serve", "--scenario", "scenarios/public_01_dev.yaml",
            "--run-dir", str(tmp_path / "run"), "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    client = None
    try:
        deadline = time.time() + 180
        ready = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    ready = httpx.get(f"{url}/sim/state", timeout=5).json()
                    if ready.get("scenario") == "public_01_dev":
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        assert ready is not None and ready["scenario"] == "public_01_dev"
        client = SimClient(url)
        documents = {item["workflow_id"]: item for item in client.workflows()}
        renewal = documents["nw_renewal"]["governance"]["auto_apply"]
        kyc = documents["nw_kyc"]["governance"]["auto_apply"]
        cancel = documents["ks_cancel"]["governance"]["auto_apply"]
        assert renewal["enabled"] is True
        assert set(renewal["allowed_dims"]) == {"schedule", "caller_id"}
        assert renewal["max_shift"] == 0.5
        assert kyc["enabled"] is False
        assert cancel["enabled"] is True
        assert set(cancel["allowed_dims"]) == {"schedule", "caller_id", "welcome_message", "voice"}
        assert cancel["max_shift"] == 0.2
        current = client.get_config("ks_cancel")
        assert current["version"] == 1
        proposed = copy.deepcopy(current["config"])
        start = int(documents["ks_cancel"]["dialling_window"][0])
        proposed["schedule"] = {
            "hour_weights": {"default": {str(start): 1}},
            "lead_preferred_hour": {},
        }
        decision = evaluate_policy(
            documents["ks_cancel"],
            current,
            proposed,
            sim_ts=ready["ts"],
            pending_approvals=0,
            recent_requests=0,
            evidence_version=current["version"],
        )
        assert decision["decision"] == "auto_apply_eligible"
        applied = client.auto_apply(
            "ks_cancel",
            {
                "proposal_id": "gov-public01-schedule",
                "base_version": current["version"],
                "proposed_config": proposed,
                "rationale": "Integration check of a schedule change inside the fetched auto-apply policy.",
                "evidence": {"evidence_ids": ["integration"]},
            },
        )
        assert applied.ok is True
        fresh = client.get_config("ks_cancel")
        history = client.config_history("ks_cancel")
        assert fresh["version"] == 2
        assert fresh["actor"] == "controller_auto"
        assert history[-1]["proposal_id"] == "gov-public01-schedule"
        assert history[-1]["version"] == 2
        kyc_current = client.get_config("nw_kyc")
        kyc_proposed = copy.deepcopy(kyc_current["config"])
        start = int(documents["nw_kyc"]["dialling_window"][0])
        kyc_proposed["schedule"] = {
            "hour_weights": {"default": {str(start): 1}},
            "lead_preferred_hour": {},
        }
        kyc_decision = evaluate_policy(
            documents["nw_kyc"],
            kyc_current,
            kyc_proposed,
            sim_ts=ready["ts"],
            pending_approvals=0,
            recent_requests=0,
            evidence_version=kyc_current["version"],
        )
        assert kyc_decision["decision"] == "approval_required"
        assert client.get_config("nw_kyc")["version"] == 1
    finally:
        if client is not None:
            client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _history(proposal_id="p", version=2):
    return {"version": version, "proposal_id": proposal_id, "actor": "controller_auto"}


def test_verification_checks_each_changed_dimension():
    base = _current()["config"]
    schedule = copy.deepcopy(base)
    schedule["schedule"] = {"hour_weights": {"default": {"10": 1}}, "lead_preferred_hour": {}}
    unchanged = verify_configuration(
        schedule, base, ["schedule"], version=2, expected_version=2, history=_history()
    )
    assert unchanged["ok"] is False
    assert "schedule" in unchanged["mismatched_dims"]

    prompt = copy.deepcopy(base)
    prompt["prompt_version"] = "P2"
    prompt_miss = verify_configuration(
        prompt, base, ["prompt_version"], version=2, expected_version=2, history=_history()
    )
    assert prompt_miss["ok"] is False
    assert "prompt_version" in prompt_miss["mismatched_dims"]

    cohorts = copy.deepcopy(base)
    cohorts["cohorts"] = [{"id": "age_band:60+", "where": {"age_band": ["60+"]}}]
    cohort_miss = verify_configuration(
        cohorts, base, ["cohorts"], version=2, expected_version=2, history=_history()
    )
    assert cohort_miss["ok"] is False
    assert "cohorts" in cohort_miss["mismatched_dims"]

    moved = _moved(_current(), delta=0.2)
    weight_miss = verify_configuration(
        moved, base, ["caller_id"], version=2, expected_version=2, history=_history()
    )
    assert weight_miss["ok"] is False
    assert "caller_id" in weight_miss["mismatched_dims"]

    matched = verify_configuration(
        moved, moved, ["caller_id"], version=2, expected_version=2, history=_history()
    )
    assert matched["ok"] is True
    assert matched["mismatched_dims"] == []
    assert matched["unexpected_dims"] == []

    drifted = copy.deepcopy(moved)
    drifted["prompt_version"] = "P2"
    ambiguous = verify_configuration(
        moved, drifted, ["caller_id"], version=2, expected_version=2, history=_history()
    )
    assert ambiguous["ok"] is False
    assert "prompt_version" in ambiguous["unexpected_dims"]


def test_supervisor_edit_blocks_apply_and_a_later_tick_sees_degradation(tmp_path):
    store = _prepare(tmp_path)
    world = FakeWorld(workflow=_workflow(auto=False))
    try:
        govern(store, world, RunSettings())
        world.current = _current(version=2, actor="supervisor")
        world.approvals[0]["status"] = "approved"
        govern(store, world, RunSettings())
        assert "apply" not in world.writes
        failed = store.proposals_by_status(("failed",))
        assert failed[0]["outcome"] == "supervisor_edit"
        assert "supervisor_modification_detected" in store.audit_actions()
    finally:
        store.close()

    store = Store.open(tmp_path / "later", "unit")
    world = FakeWorld()
    proposal = build_proposal(
        world.workflow, _current(), _moved(_current(), delta=0.2), _evidence(), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "verified"
    proposal["evidence"]["baseline_n"] = 2000
    proposal["evidence"]["baseline_successes"] = 800
    proposal["evidence"]["config_snapshot"] = _current()["config"]
    proposal["verification"] = {"ok": True, "version": 2}
    store.save_proposal(proposal)
    _seed_calls(store, "D1", 40, 10, 5, version=2)
    world.current = _current(version=2, actor="controller_auto")
    world.current["config"] = _moved(_current(), delta=0.2)
    try:
        from controller.governance import _monitor_applied
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "degradation_detected" not in store.audit_actions()
        store.conn.execute("DELETE FROM call_agg")
        store.conn.execute("DELETE FROM call_group_totals")
        _seed_calls(store, "D1", 2000, 200, 100, version=2)
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "degradation_detected" in store.audit_actions()
        assert "rollback_recommended" in store.audit_actions()
    finally:
        store.close()


def test_restart_verifies_a_config_already_applied_on_the_server(tmp_path):
    store = Store.open(tmp_path / "applied", "unit")
    world = FakeWorld()
    proposed = _moved(_current(), delta=0.2)
    proposal = build_proposal(
        world.workflow, _current(), proposed, _evidence(), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "applied"
    proposal["outcome"] = "response_lost"
    store.save_proposal(proposal)
    world.current = _current(version=2, actor="controller_auto")
    world.current["config"] = proposed
    world.history.append({
        "version": 2,
        "actor": "controller_auto",
        "proposal_id": proposal["proposal_id"],
        "approval_id": None,
        "changed_dims": ["caller_id"],
    })
    try:
        govern(store, world, RunSettings())
        saved = store.get_proposal(proposal["proposal_id"])
        assert saved["status"] == "verified"
        assert saved["verification"]["ok"] is True
        assert world.writes == []
    finally:
        store.close()


def test_primary_engagement_drop_is_a_rollback_and_business_outcome_is_not(tmp_path):
    import json
    from controller.governance import _monitor_applied

    store = Store.open(tmp_path / "engage", "unit")
    world = FakeWorld()
    proposal = build_proposal(
        world.workflow, _current(), _moved(_current(), delta=0.2), _evidence(), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "verified"
    proposal["evidence"]["baselines"] = {
        "dials": 2000,
        "connected": 800,
        "engaged": 600,
        "leads_uploaded": None,
        "leads_concluded": None,
        "business_outcome_available": False,
    }
    proposal["evidence"]["config_snapshot"] = _current()["config"]
    proposal["verification"] = {"ok": True, "version": 2}
    store.save_proposal(proposal)
    _seed_calls(store, "D1", 2000, 800, 200, version=2)
    world.current = _current(version=2, actor="controller_auto")
    world.current["config"] = _moved(_current(), delta=0.2)
    try:
        _monitor_applied(store, world, RunSettings(), world.ts)
        parsed = json.loads(store.conn.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'degradation_detected'"
        ).fetchone()[0])
        assert parsed["primary_degraded"] is True
        assert parsed["guardrail_breached"] is False
        assert parsed["causal"] is False
    finally:
        store.close()

    store = Store.open(tmp_path / "business", "unit")
    world = FakeWorld()
    world.workflow["objective"] = {"primary": "business_outcome", "guardrail": {}}
    proposal = build_proposal(
        _workflow(), _current(), _moved(_current(), delta=0.2), _evidence(), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "verified"
    proposal["evidence"]["baselines"] = {
        "dials": 2000,
        "connected": 800,
        "engaged": 400,
        "business_outcome_available": False,
    }
    proposal["evidence"]["config_snapshot"] = _current()["config"]
    proposal["verification"] = {"ok": True, "version": 2}
    store.save_proposal(proposal)
    _seed_calls(store, "D1", 2000, 800, 400, version=2)
    world.current = _current(version=2, actor="controller_auto")
    world.current["config"] = _moved(_current(), delta=0.2)
    try:
        _monitor_applied(store, world, RunSettings(), world.ts)
        assert "degradation_detected" not in store.audit_actions()
    finally:
        store.close()


def _replace_calls(store: Store, rows: list[tuple[str, int, int, int]]) -> None:
    store.conn.execute("DELETE FROM call_agg")
    store.conn.execute("DELETE FROM call_group_totals")
    for caller, dials, connected, engaged in rows:
        _seed_calls(store, caller, dials, connected, engaged)


def _opportunity_audits(store: Store) -> list[dict]:
    import json
    rows = store.conn.execute(
        "SELECT detail_json FROM audit_log WHERE action = 'opportunity_detected' ORDER BY id"
    ).fetchall()
    return [json.loads(row["detail_json"]) for row in rows]


def test_repeated_opportunities_update_evidence_without_duplicate_audits(tmp_path):
    store = Store.open(tmp_path, "unit")
    world = FakeWorld()
    try:
        _replace_calls(store, [("D1", 2000, 500, 250), ("D2", 2000, 520, 260)])
        first = govern(store, world, RunSettings())
        assert first["opportunities"] == 1
        assert world.writes == []
        assert store.proposal_count() == 0
        audits = _opportunity_audits(store)
        assert len(audits) == 1
        assert audits[0]["actionable"] is False
        assert audits[0]["pickup_verdict"] == "not_significant"
        saved = store.get_opportunity(audits[0]["opportunity_id"])
        assert saved is not None
        detected_at = saved["detected_at"]
        first_sim_ts = saved["sim_ts"]
        assert saved["evidence"]["sample_n"] == 4000
        assert saved["evidence"]["pickup_comparison"]["p_value"] is not None
        assert saved["evidence"]["pickup_comparison"]["left"]["low"] is not None

        govern(store, world, RunSettings())
        assert len(_opportunity_audits(store)) == 1
        again = store.get_opportunity(audits[0]["opportunity_id"])
        assert again["detected_at"] == detected_at
        assert again["sim_ts"] == first_sim_ts

        _replace_calls(store, [("D1", 4000, 1000, 500), ("D2", 4000, 1040, 520)])
        govern(store, world, RunSettings())
        assert len(_opportunity_audits(store)) == 1
        grown = store.get_opportunity(audits[0]["opportunity_id"])
        assert grown["detected_at"] == detected_at
        assert grown["evidence"]["sample_n"] == 8000
        assert grown["evidence"]["callers"]["D1"]["dials"] == 4000
        assert grown["evidence"]["pickup_verdict"] == "not_significant"
        assert grown["evidence"]["pickup_comparison"]["p_value"] is not None
        assert "high" in grown["evidence"]["engagement_comparison"]["left"]

        _replace_calls(store, [("D1", 2000, 520, 260), ("D2", 2000, 500, 250)])
        govern(store, world, RunSettings())
        swapped = _opportunity_audits(store)
        assert len(swapped) == 2
        assert swapped[1]["worst_caller_id"] == "D2"
        assert swapped[1]["best_caller_id"] == "D1"
        assert store.get_opportunity(audits[0]["opportunity_id"])["detected_at"] == detected_at
        assert store.proposal_count() == 0

        _replace_calls(store, [("D1", 2000, 400, 360), ("D2", 2000, 800, 100)])
        govern(store, world, RunSettings())
        blocked = _opportunity_audits(store)
        assert len(blocked) == 3
        assert blocked[2]["pickup_verdict"] == "left_lower"
        assert blocked[2]["actionable"] is False
        assert "engagement" in blocked[2]["outcome"]
        assert world.writes == []

        _replace_calls(store, [("D1", 2000, 400, 200), ("D2", 2000, 800, 400)])
        opened = govern(store, world, RunSettings())
        assert opened["auto_applied"] == 1
        actionable = _opportunity_audits(store)
        assert len(actionable) == 4
        assert actionable[3]["actionable"] is True
        assert actionable[3]["pickup_verdict"] == "left_lower"
        assert world.writes == ["auto"]
        assert store.proposal_count() == 1
        kept = store.get_opportunity("caller:nw_renewal:1:D1:D2")
        assert kept["detected_at"] == detected_at
        assert kept["actionable"] is True
        assert kept["evidence"]["pickup_comparison"]["verdict"] == "left_lower"

        govern(store, world, RunSettings())
        assert world.writes == ["auto"]
        assert store.proposal_count() == 1
        follow = _opportunity_audits(store)
        assert len(follow) == 4
        assert follow[-1]["actionable"] is True
        govern(store, world, RunSettings())
        assert len(_opportunity_audits(store)) == 4
        assert world.writes == ["auto"]
        assert store.proposal_count() == 1
    finally:
        store.close()


def _audit_details(store: Store, action: str) -> list[dict]:
    import json
    rows = store.conn.execute(
        "SELECT detail_json FROM audit_log WHERE action = ? ORDER BY id",
        (action,),
    ).fetchall()
    return [json.loads(row["detail_json"]) for row in rows]


def test_supervisor_modification_is_audited_once_per_version(tmp_path):
    from controller.governance import _monitor_applied

    store = Store.open(tmp_path, "unit")
    world = FakeWorld()
    proposal = build_proposal(
        world.workflow, _current(), _moved(_current(), delta=0.2), _evidence(), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "verified"
    proposal["evidence"]["baselines"] = {"dials": 2000, "connected": 800, "engaged": 400}
    proposal["evidence"]["config_snapshot"] = _current()["config"]
    proposal["verification"] = {"ok": True, "version": 2}
    store.save_proposal(proposal)
    world.current = _current(version=4, actor="supervisor")
    try:
        for _ in range(3):
            _monitor_applied(store, world, RunSettings(), world.ts)
        first = _audit_details(store, "supervisor_modification_detected")
        assert len(first) == 1
        assert first[0]["version"] == 4
        assert first[0]["proposal_id"] == proposal["proposal_id"]
        assert "rollback_recommended" not in store.audit_actions()
        world.current = _current(version=5, actor="supervisor")
        _monitor_applied(store, world, RunSettings(), world.ts)
        _monitor_applied(store, world, RunSettings(), world.ts)
        versions = [row["version"] for row in _audit_details(store, "supervisor_modification_detected")]
        assert versions == [4, 5]
        assert world.writes == []
    finally:
        store.close()


def test_current_version_connection_health_detects_a_caller_with_few_connections(tmp_path):
    store = Store.open(tmp_path, "unit")
    try:
        _seed_calls(store, "D1", 5000, 2500, 1000, version=1, no_answer=2500)
        _seed_calls(store, "D2", 5000, 2500, 1000, version=1, no_answer=2500)
        _seed_calls(store, "D1", 2000, 80, 20, version=2, no_answer=1920)
        _seed_calls(store, "D2", 2000, 800, 400, version=2, no_answer=1200)
        found = detect_opportunities(store, _workflow(), _current(version=2, actor="controller_auto"), RunSettings())
        assert len(found) == 1
        assert found[0]["actionable"] is True
        assert found[0]["evidence"]["worst_caller_id"] == "D1"
        assert found[0]["evidence"]["best_caller_id"] == "D2"
        assert found[0]["evidence"]["config_version"] == 2
        assert found[0]["evidence"]["sample_n"] == 4000
        assert found[0]["evidence"]["callers"]["D1"]["connected"] == 80
        assert found[0]["evidence"]["callers"]["D1"]["no_answer"] == 1920
        assert found[0]["evidence"]["callers"]["D1"]["unanswered_rate"] == 1920 / 2000
        assert found[0]["evidence"]["health_signal"]["fields"] == ["connected", "disposition"]
        assert found[0]["evidence"]["health_signal"]["source"] == "GET /events/chunks/{n}"
        weights = found[0]["proposed_config"]["allocations"]["default"]["caller_id"]
        assert weights.get("D1", 0) < 0.5
        assert found[0]["evidence"]["causal"] is False

        store.conn.execute("DELETE FROM call_agg")
        store.conn.execute("DELETE FROM call_group_totals")
        _seed_calls(store, "D1", 2000, 50, 10, version=2, no_answer=1950)
        _seed_calls(store, "D2", 2000, 300, 150, version=2, no_answer=1700)
        held = detect_opportunities(store, _workflow(), _current(version=2), RunSettings())
        assert held[0]["actionable"] is False
        assert "engagement sample floor" in held[0]["reason"]
    finally:
        store.close()


def _verified_caller_proposal(store: Store, world: FakeWorld):
    proposal = build_proposal(
        world.workflow, _current(), _moved(_current(), delta=0.2), _evidence(version=1), _impact(),
        sim_ts=world.ts, pending_approvals=0, recent_requests=0,
        rationale="Observed pickup differs. No uplift is claimed.",
    )["proposal"]
    proposal["status"] = "verified"
    proposal["evidence"]["config_version"] = 1
    proposal["evidence"]["baselines"] = {
        "dials": 2000,
        "connected": 1000,
        "engaged": 500,
        "leads_uploaded": None,
        "leads_concluded": None,
        "business_outcome_available": False,
    }
    proposal["evidence"]["config_snapshot"] = _current()["config"]
    proposal["verification"] = {"ok": True, "version": 2}
    store.save_proposal(proposal)
    return proposal


def _seed_confounded_pickup(store: Store):
    """Old hours hide the gap. The newest baseline hour, and the short new version, do not."""
    _seed_calls(store, "D1", 8000, 3200, 1600, version=1, call_date="2027-01-04", hour=9, no_answer=4800)
    _seed_calls(store, "D2", 8000, 3200, 1600, version=1, call_date="2027-01-04", hour=9, no_answer=4800)
    _seed_calls(store, "D1", 800, 320, 160, version=1, call_date="2027-01-05", hour=11, no_answer=480)
    _seed_calls(store, "D2", 800, 160, 80, version=1, call_date="2027-01-05", hour=11, no_answer=640)
    _seed_calls(store, "D1", 700, 280, 140, version=2, no_answer=420)
    _seed_calls(store, "D2", 200, 20, 10, version=2, no_answer=180)


def test_pickup_rollback_does_not_restore_weight_onto_a_worse_caller(tmp_path):
    from controller.governance import _monitor_applied

    store = Store.open(tmp_path / "keep", "unit")
    world = FakeWorld()
    try:
        proposal = _verified_caller_proposal(store, world)
        _seed_confounded_pickup(store)
        world.current = _current(weights={"D1": 0.7, "D2": 0.3}, version=2, actor="controller_auto")
        _monitor_applied(store, world, RunSettings(), world.ts)
        _monitor_applied(store, world, RunSettings(), world.ts)
        degradation = _audit_details(store, "degradation_detected")
        assert len(degradation) == 1
        assert degradation[0]["guardrail_breached"] is True
        assert degradation[0]["primary_degraded"] is False
        recommended = _audit_details(store, "rollback_recommended")
        assert len(recommended) == 1
        assert recommended[0]["outcome"] == "restore_reexposes_degraded_caller"
        assert recommended[0]["caller_health"]["gainer"] == "D2"
        assert recommended[0]["caller_health"]["sample_source"] == "recent_baseline_version"
        assert recommended[0]["caller_health"]["gainer_pickup"] == 0.2
        assert recommended[0]["causal"] is False
        assert store.proposals_by_status(("drafted",)) == []
        assert world.writes == []
        assert proposal["proposal_id"]
    finally:
        store.close()

    store = Store.open(tmp_path / "restore", "unit")
    world = FakeWorld()
    try:
        _verified_caller_proposal(store, world)
        _seed_confounded_pickup(store)
        world.current = _current(weights={"D1": 0.3, "D2": 0.7}, version=2, actor="controller_auto")
        _monitor_applied(store, world, RunSettings(), world.ts)
        recommended = _audit_details(store, "rollback_recommended")
        assert len(recommended) == 1
        assert recommended[0]["outcome"] == "auto_apply"
        drafted = store.proposals_by_status(("drafted",))
        assert len(drafted) == 1
        assert drafted[0]["evidence"].get("rollback_of")
        assert world.writes == []
    finally:
        store.close()
