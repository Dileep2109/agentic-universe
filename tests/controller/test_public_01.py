"""Phase 1 against the real public_01 simulator, over HTTP only."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_ready(url: str, proc: subprocess.Popen, log_path: Path, timeout_s: float = 180) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if proc.poll() is not None:
            raise RuntimeError(f"simulator exited {proc.returncode}: {log_text}")
        try:
            health = httpx.get(f"{url}/health", timeout=2)
            if health.status_code == 200:
                state = httpx.get(f"{url}/sim/state", timeout=5)
                if state.status_code == 200 and state.json()["scenario"] == "public_01_dev":
                    return state.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise RuntimeError(f"simulator did not become ready. output:\n{log_text}")


def _count_http_events(url: str, kind: str) -> int:
    chunks = httpx.get(f"{url}/events/chunks", params={"kind": kind, "after": -1}, timeout=60).json()
    total = 0
    for chunk in chunks:
        text = httpx.get(f"{url}/events/chunks/{chunk['chunk']}", timeout=120).text
        total += sum(1 for line in text.splitlines() if line.strip())
    return total


def _run_controller(url: str, state_dir: Path, max_advances: int) -> tuple[dict, str]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "controller",
            "--url",
            url,
            "--state-dir",
            str(state_dir),
            "--max-advances",
            str(max_advances),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout), proc.stderr


def test_public_01_ingest_and_restart(tmp_path):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    run_dir = tmp_path / "sim-run"
    log_path = tmp_path / "sim.log"
    log_file = log_path.open("w", encoding="utf-8", buffering=1)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sim",
            "serve",
            "--scenario",
            "scenarios/public_01_dev.yaml",
            "--run-dir",
            str(run_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    state_dir = tmp_path / "controller-state"
    try:
        before = _wait_ready(url, proc, log_path)
        assert before["sim_minute"] == 0
        assert before["last_event_id"] > 0

        history_http = _count_http_events(url, "history")
        first, _ = _run_controller(url, state_dir, max_advances=40)
        after_first = httpx.get(f"{url}/sim/state", timeout=10).json()

        assert first["scenario"] == "public_01_dev"
        assert first["sim_minute"] == 600
        assert after_first["sim_minute"] == 600
        assert first["last_event_id"] == after_first["last_event_id"]
        assert first["pending_advance_from_minute"] is None
        assert first["counts"]["history|lead"] > 0
        assert first["counts"]["history|call"] > 0
        assert first["counts"].get("events|lead", 0) > 0
        assert first["counts"].get("events|call", 0) > 0
        history_db = sum(n for key, n in first["counts"].items() if key.startswith("history|"))
        assert history_db == history_http
        assert first["call_totals"]["dials"] == (
            first["counts"]["history|call"] + first["counts"]["events|call"]
        )
        for kpi in first["kpis"].values():
            assert set(kpi) >= {"pickup_rate", "engagement_rate", "business_outcome"}
            assert "blended" not in kpi

        stalled, _ = _run_controller(url, state_dir, max_advances=0)
        after_stall = httpx.get(f"{url}/sim/state", timeout=10).json()
        assert after_stall["sim_minute"] == 600
        assert stalled["sim_minute"] == 600
        assert stalled["last_event_id"] == first["last_event_id"]
        assert stalled["call_totals"] == first["call_totals"]
        assert stalled["leads"] == first["leads"]
        assert stalled["counts"] == first["counts"]

        second, _ = _run_controller(url, state_dir, max_advances=2)
        after_second = httpx.get(f"{url}/sim/state", timeout=10).json()
        assert after_second["sim_minute"] == 630
        assert second["sim_minute"] == 630
        assert second["last_event_id"] == after_second["last_event_id"]
        new_calls = second["call_totals"]["dials"] - first["call_totals"]["dials"]
        assert new_calls == second["counts"]["events|call"] - first["counts"]["events|call"]
        assert new_calls > 0
        assert second["leads"] >= first["leads"]

        evidence = {
            "history_events_http": history_http,
            "history_events_db": history_db,
            "after_40_ticks": {
                "sim_minute": first["sim_minute"],
                "last_event_id": first["last_event_id"],
                "leads": first["leads"],
                "counts": first["counts"],
                "call_totals": first["call_totals"],
                "kpis": first["kpis"],
            },
            "restart_without_advance": {
                "sim_minute": stalled["sim_minute"],
                "last_event_id": stalled["last_event_id"],
                "call_totals": stalled["call_totals"],
            },
            "two_more_ticks": {
                "sim_minute": second["sim_minute"],
                "last_event_id": second["last_event_id"],
                "new_calls": new_calls,
                "call_totals": second["call_totals"],
            },
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)
        log_file.close()


def test_public_01_history_snapshot_submits_nothing(tmp_path):
    """History-only analytics. The controller must not advance time or change config."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    run_dir = tmp_path / "sim-run"
    log_path = tmp_path / "sim.log"
    log_file = log_path.open("w", encoding="utf-8", buffering=1)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sim",
            "serve",
            "--scenario",
            "scenarios/public_01_dev.yaml",
            "--run-dir",
            str(run_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    state_dir = tmp_path / "controller-state"
    try:
        before = _wait_ready(url, proc, log_path)
        assert before["sim_minute"] == 0
        summary, _stderr = _run_controller(url, state_dir, max_advances=0)
        after = httpx.get(f"{url}/sim/state", timeout=10).json()
        approvals = httpx.get(f"{url}/approvals", timeout=10).json()
        assert after["sim_minute"] == 0
        assert summary["sim_minute"] == 0
        assert approvals == []
        assert summary["pending_advance_from_minute"] is None

        configs = {}
        for workflow in httpx.get(f"{url}/workflows", timeout=10).json():
            workflow_id = workflow["workflow_id"]
            config = httpx.get(f"{url}/workflows/{workflow_id}/config", timeout=10).json()
            history = httpx.get(f"{url}/workflows/{workflow_id}/config/history", timeout=10).json()
            configs[workflow_id] = {
                "version": config["version"],
                "actor": config.get("actor"),
                "history_entries": len(history),
            }
            assert config["version"] == 1
            assert config.get("actor") == "business_default"
            assert [entry["version"] for entry in history] in ([], [1])
            assert all(entry.get("actor") == "business_default" for entry in history)
            assert all(not entry.get("proposal_id") and not entry.get("approval_id") for entry in history)

        from controller.settings import FEATURE_VALUES
        from controller.store import Store

        store = Store.open(state_dir, "public_01_dev")
        try:
            assert store.proposal_count() == 0
            assert store.conn.execute("SELECT COUNT(*) AS n FROM config_changes").fetchone()["n"] == 0
            snapshot = store.latest_snapshot()
        finally:
            store.close()
        assert snapshot is not None
        assert snapshot["incidents"] == []
        assert len(snapshot["workflows"]) == 3
        evidence_workflows = []
        for item in snapshot["workflows"]:
            history = item["baselines"]["history"]
            assert history["dials"] > 0
            assert history["connected"] > 0
            assert history["leads_uploaded"] > 0
            assert history["pickup_rate"] == history["connected"] / history["dials"]
            assert history["engagement_rate"] == history["engaged"] / history["connected"]
            assert history["business_outcome"] == history["leads_concluded"] / history["leads_uploaded"]
            definitions = item["shared_cohorts"]["definitions"]
            evidence = item["shared_cohorts"]["selection_evidence"]
            assert len(definitions) <= 6
            if definitions:
                assert evidence["family"] == "cohort_selection"
                assert evidence["p_value"] < evidence["adjusted_alpha"]
                assert evidence["gap"] >= 0.02
                assert len(evidence["compared_levels"]) == 2
                assert evidence["left"]["n"] > 0 and evidence["right"]["n"] > 0
            else:
                assert evidence is None
            for row in definitions:
                assert row["cohort_id"] not in ("default", "missing")
                assert row["where"]
                for feature, values in row["where"].items():
                    assert feature in FEATURE_VALUES
                    assert values and all(value in FEATURE_VALUES[feature] for value in values)
                assert "independently demonstrated" in row["reason"]
                level = row["cohort_id"].split(":", 1)[1]
                if level not in evidence["compared_levels"]:
                    assert "was not in the selecting pair" in row["reason"]
                assert row["support_dials"] >= 800
            for finding in item["versus_history"]:
                assert finding["causal"] is False
                assert "observational" in finding["observational_warning"]
                assert "not a causal" in finding["observational_warning"]
            assert "excluded" in item["shared_cohorts"]["missing_features"]["policy"]
            for finding in item["confirmed"]:
                assert finding["status"] == "confirmed"
                assert "not an incident" in finding["note"]
                assert finding["evidence"].get("p_value") is not None
            for finding in item["observations"]:
                assert finding["status"] in ("observation", "insufficient")
            evidence_workflows.append({
                "workflow_id": item["workflow_id"],
                "history": {
                    "dials": history["dials"],
                    "connected": history["connected"],
                    "engaged": history["engaged"],
                    "leads_uploaded": history["leads_uploaded"],
                    "leads_concluded": history["leads_concluded"],
                    "pickup_rate": history["pickup_rate"],
                    "engagement_rate": history["engagement_rate"],
                    "business_outcome": history["business_outcome"],
                },
                "cohorts": [row["cohort_id"] for row in definitions],
                "confirmed": len(item["confirmed"]),
                "observations": len(item["observations"]),
                "within_variation": len(item["within_variation"]),
            })
        print(json.dumps({
            "sim_minute": after["sim_minute"],
            "approvals": approvals,
            "configs": configs,
            "proposals": 0,
            "incidents": snapshot["incidents"],
            "workflows": evidence_workflows,
        }, indent=2, sort_keys=True))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)
        log_file.close()
