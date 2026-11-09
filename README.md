# Outcome-first controller

Python controller for Formi's outbound voice workflows. It talks to the simulator only over HTTP. This repository does not contain the simulator, the scenario files, or the stakeholder memos. Those are supplied separately. Hidden scenarios use the same HTTP schema and different truth, so this controller does not read scenario files or simulator internals.

## What it does

Each pass reads simulator state, ingests any new event chunks, refreshes workflow documents, publishes a supervisor snapshot when one is due, reviews experiment designs without applying them, and runs governance before the next advance. Pickup (connected/dials), engagement (engaged/connected), and business outcome (leads concluded / leads uploaded) stay separate. A configuration change is sent only when the fetched workflow policy allows that exact diff. An LLM does not choose the winner. Experiment designs stay non-executable, and `causal` stays false.

The supervisor snapshot is rebuilt on the first pass, after a configuration write, when the simulated hour changes, and on the pass that stops. Grouped call and lead totals are updated in the same database transaction as the events they summarize.

Governance lives in `controller/policy.py`, `controller/impact.py`, and `controller/governance.py`. The HTTP contract is the separately supplied simulator document, not a copy kept in this tree.

## Layout

| Path | Role |
|---|---|
| `controller/loop.py` | Tick order, advance, restart reconciliation |
| `controller/client.py` | Documented HTTP calls. Reads may be retried. Writes are not retried blindly. |
| `controller/ingest.py` | Chunk download and one-time apply |
| `controller/store.py` | One SQLite file per scenario, WAL |
| `controller/analytics.py`, `cohorts.py`, `monitor.py` | Counts, cohorts, caller-id comparison |
| `controller/experiments.py` | Designs only. They are not applied. |
| `controller/supervisor.py` | Snapshot document stored locally |
| `controller/settings.py` | Statistical thresholds. They are not weakened to force a proposal. |
| `docs/GOVERNANCE.md` | Policy, verification, and rollback rules |
| `reports/` | Grade output for public_01 and public_02, and the partial public_03 measurement |
| `tests/controller/` | Controller tests |

## Setup

Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

Dependencies: `numpy`, `pyyaml`, `fastapi`, `uvicorn`, `httpx`, `pytest`. There is no Redis and no extra service. `fastapi` and `uvicorn` are installed with the assignment dependency set; this controller is a client of the simulator, not a second server.

## Run

Start the separately supplied simulator, then:

```bash
./run.sh http://127.0.0.1:8700
```

`run.sh` runs until the connected scenario finishes. It does not take a scenario name and it does not grade. State goes to `./state` unless `CONTROLLER_STATE_DIR` is set. The database file is `{state_dir}/{scenario}.sqlite` in WAL mode. `state/`, `runs/`, and `*.sqlite` are gitignored.

Equivalent direct invocation:

```bash
python -m controller --url http://127.0.0.1:8700 --state-dir state
```

`--max-advances` stops after a fixed number of confirmed advances. `run.sh` does not set it. `--max-advances 0` ingests and decides once and does not advance.

Grade with the supplied simulator, against a baseline run you produced the same way, and write the markdown into `reports/`. This repository already contains:

- `reports/public_01.md` — completed public_01 grade. Zero applied changes.
- `reports/public_02.md` — completed public_02 grade. Two caller-id auto-applies, one later marked harmful by the grader.
- `reports/public_03_load_measurements.md` — partial load run. One simulated day took 34 min 29.8 s. The 10-minute target was not met. `sim grade` was not run.

A later benchmark on a copy of that database reduced one snapshot-shaped query from 125.413 s to 0.127 s. That is not a completed load test, and it is not a measured day under 10 minutes.

## Tests

Recorded controller suite, not re-run for this document:

```text
python -m pytest -q tests/controller
116 passed, 1 warning, 119.78s
```

Most tests use a temporary database or a fake HTTP client and do not need the simulator.

These tests start the separately supplied simulator and need `scenarios/public_01_dev.yaml` on the process working directory. They are kept. They fail if that package and scenario are not available:

- `tests/controller/test_public_01.py`
- `tests/controller/test_governance.py::test_public_01_policy_matches_the_simulator_and_a_legal_auto_apply_verifies`

The second test posts one in-policy `ks_cancel` schedule change to a simulator it starts itself. That is an integration check. It is not the graded public_01 scenario, which applied zero changes.

`tests/test_sim.py` is the supplied simulator smoke test. It imports `sim` and loads `scenarios/public_01_dev.yaml`. It is not part of this repository. Run it from the simulator tree if you need it.

## What this does not claim

- No causal A/B test, and no claim that a planned hash arm was exposure.
- No process-kill of a live scenario followed by a restart that finished correctly. Restart and lost-response behavior are covered by tests.
- The public_03 10-minute day was not met on the measured run. Code added after that run was not validated by another full load test.
- Decision thresholds in `controller/settings.py` are the production floors (800 dials, 400 connected, alpha 0.05, minimum absolute gap 0.02, guardrail confidence 0.90). Tests may override them locally. The live loop does not.
