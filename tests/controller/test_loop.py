"""Restart and lost-advance behaviour against an in-memory simulator."""

import ast
from pathlib import Path

from controller.client import AmbiguousResponse
from controller.loop import run
from controller.settings import RunSettings
from controller.store import Store


ROOT = Path(__file__).resolve().parents[2]


def _ts(minute: int) -> str:
    hour, rest = divmod(minute, 60)
    return f"2027-01-04T{hour:02d}:{rest:02d}:00+05:30"


class FakeSim:
    def __init__(self):
        self.minute = 0
        self.finished = False
        self.chunks: list[dict] = []
        self.bodies: dict[int, str] = {}
        self.advance_calls = 0
        self.drop = None
        self.writes: list[str] = []
        self._event_id = 0
        self._seed_history()

    def _seed_history(self) -> None:
        self._emit("history", [
            {
                "type": "campaign_created",
                "ts": "2026-12-21T08:00:00+05:30",
                "tenant_id": "northwind_life",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "leads": 1,
                "config_version_at_creation": 1,
            },
            {
                "type": "lead",
                "ts": "2026-12-21T08:00:00+05:30",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "lead_id": "nw_renewal:l00000001",
                "person_id": "nw_renewal:p0000001",
                "features": {"lang": "hi", "city_tier": "1", "age_band": "18-30", "value_band": "low"},
            },
            self._call("2026-12-21T09:00:00+05:30", connected=False, engaged=False, disposition="no_answer"),
        ])

    def _call(self, ts: str, **kw) -> dict:
        return {
            "type": "call",
            "ts": ts,
            "tenant_id": "northwind_life",
            "workflow_id": "nw_renewal",
            "campaign_id": "nw_renewal:c0001",
            "lead_id": "nw_renewal:l00000001",
            "person_id": "nw_renewal:p0000001",
            "attempt_no": 1,
            "campaign_day_index": 0,
            "config_version": 1,
            "cohort_id": "default",
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
            "qa": None,
            **kw,
        }

    def _emit(self, kind: str, events: list[dict]) -> dict:
        lines = []
        for event in events:
            self._event_id += 1
            event = {**event, "event_id": self._event_id}
            lines.append(__import__("json").dumps(event))
        meta = {
            "chunk": len(self.chunks),
            "kind": kind,
            "events": len(events),
            "first_event_id": self._event_id - len(events) + 1,
            "last_event_id": self._event_id,
            "until_ts": _ts(self.minute),
        }
        self.chunks.append(meta)
        self.bodies[meta["chunk"]] = "\n".join(lines) + "\n"
        return meta

    def state(self) -> dict:
        return {
            "scenario": "fake",
            "sim_minute": self.minute,
            "ts": _ts(self.minute),
            "end_minute": 10_000,
            "finished": self.finished,
            "tick_minutes": 15,
            "credits_remaining": 40,
            "chunks": len(self.chunks),
            "last_event_id": self._event_id,
        }

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        return [chunk for chunk in self.chunks if chunk["kind"] == kind and chunk["chunk"] > after]

    def get_chunk(self, n: int) -> str:
        return self.bodies[n]

    def advance(self, minutes: int) -> dict:
        self.advance_calls += 1
        if self.drop == "before":
            self.drop = None
            raise AmbiguousResponse(0, "transport", "reset before the world moved")
        self.minute += minutes
        self._emit("events", [
            self._call(_ts(self.minute), connected=True, engaged=False, disposition="short_hangup"),
        ])
        if self.drop == "after":
            self.drop = None
            raise AmbiguousResponse(0, "transport", "reset after the world moved")
        return {"sim_minute": self.minute, "ts": _ts(self.minute), "finished": False, "new_chunks": []}

    def workflows(self) -> list[dict]:
        return [{
            "workflow_id": "nw_renewal",
            "tenant_id": "northwind_life",
            "current_version": 1,
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
        }]

    def get_config(self, workflow_id: str) -> dict:
        return {
            "workflow_id": workflow_id,
            "version": 1,
            "actor": "business_default",
            "applied_at": None,
            "config": {"prompt_version": "P1", "cohorts": [], "allocations": {}, "schedule": {}},
        }

    def request_approval(self, body: dict) -> dict:
        self.writes.append("approval")
        return body

    def auto_apply(self, workflow_id: str, body: dict) -> dict:
        self.writes.append("auto")
        return body

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict:
        self.writes.append("apply")
        return {}

    def close(self) -> None:
        return None


def test_restart_does_not_duplicate_events_or_extra_advance(tmp_path):
    sim = FakeSim()
    settings = RunSettings(max_consecutive_advance_failures=5)
    first = run(state_dir=tmp_path, max_advances=2, settings=settings, client=sim)
    assert sim.advance_calls == 2
    assert sim.minute == 30
    assert first["call_totals"]["dials"] == 3  # 1 history + 2 live
    assert first["leads"] == 1
    assert first["counts"]["history|call"] == 1
    assert first["counts"]["events|call"] == 2

    sim.drop = "after"
    second = run(state_dir=tmp_path, max_advances=1, settings=settings, client=sim)
    assert sim.advance_calls == 3
    assert sim.minute == 45
    assert second["call_totals"]["dials"] == 4
    assert second["last_event_id"] == sim.state()["last_event_id"]

    third = run(state_dir=tmp_path, max_advances=0, settings=settings, client=sim)
    assert sim.advance_calls == 3
    assert sim.minute == 45
    assert third["call_totals"] == second["call_totals"]
    assert third["leads"] == second["leads"]
    assert third["last_event_id"] == second["last_event_id"]
    assert third["pending_advance_from_minute"] is None
    assert sim.writes == []


def test_lost_advance_that_did_not_land_is_retried_once(tmp_path):
    sim = FakeSim()
    sim.drop = "before"
    summary = run(
        state_dir=tmp_path,
        max_advances=1,
        settings=RunSettings(max_consecutive_advance_failures=5),
        client=sim,
    )
    assert sim.advance_calls == 2
    assert sim.minute == 15
    assert summary["call_totals"]["dials"] == 2
    reopened = Store.open(tmp_path, "fake")
    assert "advance_not_landed" in reopened.audit_actions()
    assert "advance_confirmed" in reopened.audit_actions()
    reopened.close()


def test_phase1_loop_has_no_config_writes_and_no_sim_import():
    loop_tree = ast.parse((ROOT / "controller" / "loop.py").read_text(encoding="utf-8"))
    called = {
        node.attr
        for node in ast.walk(loop_tree)
        if isinstance(node, ast.Attribute)
    }
    names = {node.id for node in ast.walk(loop_tree) if isinstance(node, ast.Name)}
    used = called | names
    assert "request_approval" not in used
    assert "auto_apply" not in used
    assert "apply_approval" not in used
    assert "suggest_fixes" not in used
    assert "start_validation" not in used
    assert "evaluate_design" not in used
    assert "evaluate_persisted" not in used
    for path in (ROOT / "controller").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "sim" for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "sim"
    script = (ROOT / "run.sh").read_text(encoding="utf-8")
    assert "python -m controller" in script
    assert "naive_controller" not in script


class GovernSim(FakeSim):
    """FakeSim plus the read and write methods governance calls."""

    def __init__(self):
        super().__init__()
        self.end_minute = 10_000
        self.approval_reads = []
        self.config = {
            "cohorts": [],
            "allocations": {
                "default": {
                    "welcome_message": {"W1": 1.0},
                    "voice": {"V1": 1.0},
                    "caller_id": {"D1": 0.5, "D2": 0.5},
                }
            },
            "prompt_version": "P1",
            "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
        }
        self.version = 1
        self.history = [{"version": 1, "proposal_id": None, "actor": "business_default"}]
        self.burst_sent = False

    def state(self):
        body = super().state()
        body["end_minute"] = self.end_minute
        return body

    def workflows(self):
        return [{
            "workflow_id": "nw_renewal",
            "tenant_id": "northwind_life",
            "current_version": self.version,
            "mid_flight": "allowed",
            "dialling_window": [9, 21],
            "options": {
                "welcome_message": ["W1", "W2"],
                "voice": ["V1", "V2"],
                "caller_id": ["D1", "D2"],
                "prompt_version": ["P1", "P2"],
            },
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
            "governance": {
                "auto_apply": {"enabled": True, "allowed_dims": ["caller_id", "schedule"], "max_shift": 0.5},
                "approver_hours": [10, 19],
                "approver_latency_minutes": [30, 240],
                "proposal_ttl_hours": 24,
                "apply_window_hours": 2,
                "max_requests_per_24h": 3,
            },
        }]

    def get_config(self, workflow_id):
        return {
            "workflow_id": workflow_id,
            "version": self.version,
            "actor": "business_default" if self.version == 1 else "controller_auto",
            "applied_at": None,
            "config": self.config,
        }

    def list_approvals(self, workflow_id=None):
        self.approval_reads.append(self.minute)
        return []

    def find_approval_by_proposal(self, proposal_id, workflow_id=None):
        return None

    def find_config_by_proposal(self, workflow_id, proposal_id):
        for row in self.history:
            if row.get("proposal_id") == proposal_id:
                return row
        return None

    def auto_apply(self, workflow_id, body):
        self.writes.append(("auto", self.minute))
        self.version += 1
        self.config = body["proposed_config"]
        self.history.append({
            "version": self.version,
            "proposal_id": body["proposal_id"],
            "actor": "controller_auto",
            "changed_dims": ["caller_id"],
        })
        from controller.client import ReconciledWrite
        return ReconciledWrite(True, False, {"workflow_id": workflow_id, "version": self.version}, "applied")

    def advance(self, minutes):
        self.advance_calls += 1
        self.minute += minutes
        events = []
        if not self.burst_sent:
            self.burst_sent = True
            events.extend(self._variant_calls("D1", connected=4, engaged=2, dials=20))
            events.extend(self._variant_calls("D2", connected=16, engaged=8, dials=20))
        else:
            events.append(self._call(_ts(self.minute), connected=False, engaged=False, disposition="no_answer"))
        self._emit("events", events)
        return {"sim_minute": self.minute, "ts": _ts(self.minute), "finished": self.minute >= self.end_minute, "new_chunks": []}

    def _variant_calls(self, caller_id: str, connected: int, engaged: int, dials: int) -> list[dict]:
        events = []
        for index in range(dials):
            events.append(self._call(
                _ts(self.minute),
                connected=index < connected,
                engaged=index < engaged,
                disposition="short_hangup" if index < connected else "no_answer",
                variant={
                    "welcome_message": "W1",
                    "voice": "V1",
                    "caller_id": caller_id,
                    "prompt_version": "P1",
                },
            ))
        return events


def test_max_advances_zero_governs_once_and_does_not_advance(tmp_path):
    sim = GovernSim()
    summary = run(state_dir=tmp_path, max_advances=0, settings=RunSettings(), client=sim)
    assert sim.advance_calls == 0
    assert sim.minute == 0
    assert sim.approval_reads
    assert sim.writes == []
    assert summary["governance_ticks"] == 1
    assert summary["governance"]["submitted"] == 0
    assert summary["governance"]["auto_applied"] == 0


def test_intermediate_tick_can_propose_before_the_scenario_ends(tmp_path):
    sim = GovernSim()
    settings = RunSettings()
    settings = RunSettings(
        thresholds=settings.thresholds.overridden(
            pickup_min_dials=20,
            engagement_min_connected=4,
            min_abs_change=0.2,
        )
    )
    summary = run(state_dir=tmp_path, max_advances=2, settings=settings, client=sim)
    assert sim.advance_calls == 2
    assert sim.minute == 30
    assert sim.minute < sim.end_minute
    assert sim.writes
    assert sim.writes[0][0] == "auto"
    assert sim.writes[0][1] == 15
    assert summary["governance_ticks"] == 3
    reopened = Store.open(tmp_path, "fake")
    try:
        assert reopened.proposal_count() == 1
    finally:
        reopened.close()


def test_governance_runs_on_ticks_that_do_not_rebuild_the_snapshot(tmp_path):
    """Four advances cross one simulated hour. Governance runs every pass."""
    sim = GovernSim()
    settings = RunSettings(
        thresholds=RunSettings().thresholds.overridden(
            pickup_min_dials=20,
            engagement_min_connected=4,
            min_abs_change=0.2,
        )
    )
    summary = run(state_dir=tmp_path, max_advances=4, settings=settings, client=sim)
    assert summary["governance_ticks"] == 5
    assert sim.approval_reads
    assert sim.writes
    assert sim.writes[0][1] == 15
    store = Store.open(tmp_path, "fake")
    try:
        rows = store.conn.execute(
            "SELECT sim_minute FROM supervisor_snapshots ORDER BY id"
        ).fetchall()
    finally:
        store.close()
    minutes = [int(row["sim_minute"]) for row in rows]
    assert len(minutes) < summary["governance_ticks"]
    assert minutes[0] == 0
    assert minutes[-1] == 60
