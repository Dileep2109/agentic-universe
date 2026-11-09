"""Tick loop: ingest, decide, then advance 15 simulated minutes.

Each pass uses the events already stored. It refreshes workflow configuration,
publishes the supervisor snapshot, reviews experiment designs, and runs
governance before the simulator is advanced. Experiment designs are not
executed and are not treatment exposure. Governance writes go through
``govern``, which reads the fetched policy before any approval or auto-apply.

``POST /sim/advance`` has no idempotency key. Before the call we store the
server minute. If the response is lost, the next start reads ``/sim/state``
and advances again only when that minute did not move. ``--max-advances 0``
still ingests and decides once, and does not call advance.
"""

from __future__ import annotations

import logging

from .client import AmbiguousResponse, SimClient, SimError
from .experiments import review_experiments
from .governance import govern
from .ingest import sync_all
from .settings import RunSettings
from .store import Store, WorldReset
from .supervisor import publish_snapshot

log = logging.getLogger(__name__)

# The full snapshot rebuilds cohorts and stores a large document. Governance does
# not read that document; it queries the latest calls itself. Refresh the
# snapshot on the first pass, after a configuration write, when the simulated
# hour changes, and on the pass that stops the loop.
SNAPSHOT_INTERVAL_MINUTES = 60


def run(
    url: str | None = None,
    state_dir: str = "state",
    tick_minutes: int = 15,
    max_advances: int | None = None,
    settings: RunSettings | None = None,
    client: SimClient | None = None,
) -> dict:
    settings = settings or RunSettings()
    own_client = client is None
    if client is None:
        if not url:
            raise ValueError("url is required")
        client = SimClient(url, settings)
    assert client is not None
    try:
        server = client.state()
        store = Store.open(state_dir, server["scenario"])
        try:
            store.ensure_same_world(server)
            _resolve_pending(client, store, server)
            sync_all(client, store)
            server = client.state()
            if store.pending_advance_from_minute is None:
                store.adopt_clock(server)
            _refresh_workflows(client, store)
            store.record_metric(server, {"event": "caught_up"})
            log.info(
                "caught up scenario=%s sim_minute=%s last_event_id=%s leads=%s",
                server["scenario"],
                server["sim_minute"],
                store.last_event_id,
                store.lead_count(),
            )

            advances = 0
            failures = 0
            decisions = 0
            snapshot = None
            plan = {"summary": {}}
            governance = {"mode": "not_run"}
            last_snapshot_minute: int | None = None
            publish_next = True
            while True:
                server = client.state()
                stopping = bool(server["finished"]) or (
                    max_advances is not None and advances >= max_advances
                )
                if server["finished"]:
                    sync_all(client, store)
                    store.adopt_clock(server)
                    _refresh_workflows(client, store)
                    store.record_metric(server, {"event": "finished"})
                    log.info("scenario finished at %s", server.get("ts"))
                publish = _snapshot_due(
                    server,
                    stopping=stopping,
                    last_snapshot_minute=last_snapshot_minute,
                    publish_next=publish_next,
                )
                snapshot, plan, governance = _decide(
                    client, store, settings, publish=publish, previous=snapshot
                )
                if publish:
                    last_snapshot_minute = int(server["sim_minute"])
                    publish_next = False
                if any(governance.get(key) for key in ("submitted", "auto_applied", "applied", "verified")):
                    publish_next = True
                decisions += 1
                if stopping:
                    if max_advances is not None and advances >= max_advances and not server["finished"]:
                        log.info("stopping after %s confirmed advances", advances)
                    break
                moved = _advance_once(client, store, tick_minutes, server)
                if moved:
                    advances += 1
                    failures = 0
                else:
                    failures += 1
                    if failures >= settings.max_consecutive_advance_failures:
                        raise RuntimeError(
                            f"{failures} advances in a row did not move sim_minute from {server['sim_minute']}"
                        )
            store.record_metric(client.state(), {"event": "loop_exit", "advances": advances, "decisions": decisions})
            summary = store.summary()
            summary["experiments"] = plan["summary"]
            summary["governance"] = governance
            summary["governance_ticks"] = decisions
            summary["shared_cohorts"] = {
                item["workflow_id"]: [row["cohort_id"] for row in item["shared_cohorts"]["definitions"]]
                for item in snapshot["workflows"]
            }
            summary["confirmed_findings"] = sum(len(item["confirmed"]) for item in snapshot["workflows"])
            log.info("checkpoint %s", summary)
            return summary
        finally:
            store.close()
    finally:
        if own_client:
            client.close()


def _snapshot_due(
    server: dict,
    *,
    stopping: bool,
    last_snapshot_minute: int | None,
    publish_next: bool,
) -> bool:
    if stopping or publish_next or last_snapshot_minute is None:
        return True
    return int(server["sim_minute"]) // SNAPSHOT_INTERVAL_MINUTES != last_snapshot_minute // SNAPSHOT_INTERVAL_MINUTES


def _decide(
    client: SimClient,
    store: Store,
    settings: RunSettings,
    *,
    publish: bool,
    previous: dict | None,
) -> tuple[dict, dict, dict]:
    """Experiment review and governance on the events already ingested.

    The supervisor snapshot is rebuilt when ``publish`` is set. Other passes
    keep the previous snapshot and still run governance on the latest rows.
    """
    if publish or previous is None:
        _refresh_workflows(client, store)
        snapshot = publish_snapshot(store, settings)
    else:
        snapshot = previous
    try:
        credits = client.state().get("credits_remaining")
    except (AmbiguousResponse, SimError):
        credits = None
    plan = review_experiments(store, settings, credits_remaining=credits)
    governance = govern(store, client, settings)
    try:
        fresh = client.state()
    except (AmbiguousResponse, SimError):
        fresh = {"sim_minute": None, "ts": None, "last_event_id": store.last_event_id, "credits_remaining": credits}
    store.record_metric(
        fresh,
        {
            "event": "governed",
            "submitted": governance.get("submitted"),
            "auto_applied": governance.get("auto_applied"),
            "verified": governance.get("verified"),
        },
    )
    return snapshot, plan, governance


def _advance_once(client: SimClient, store: Store, tick_minutes: int, server: dict) -> bool:
    tick = int(server["tick_minutes"])
    if tick_minutes <= 0 or tick_minutes % tick != 0:
        raise ValueError(f"tick_minutes must be a positive multiple of {tick}")
    before = int(server["sim_minute"])
    store.mark_pending_advance(before)
    try:
        client.advance(tick_minutes)
    except AmbiguousResponse as exc:
        log.warning("advance response lost (%s); reconciling against /sim/state", exc.code)
        landed = _resolve_pending(client, store)
        return landed
    except SimError as exc:
        if exc.code == "finished":
            store.clear_pending_advance()
            fresh = client.state()
            sync_all(client, store)
            store.adopt_clock(fresh)
            return int(fresh["sim_minute"]) > before
        # A definite 4xx means the simulator rejected the call and did not move.
        store.clear_pending_advance()
        raise
    changed = sync_all(client, store)
    fresh = client.state()
    store.adopt_clock(fresh)
    moved = int(fresh["sim_minute"]) > before
    store.audit(
        "advance_confirmed",
        {"from_minute": before, "sim_minute": fresh["sim_minute"], "moved": moved},
        sim_ts=fresh.get("ts"),
    )
    if changed:
        _refresh_workflows(client, store, only=set(changed))
    log.info("advanced %s -> %s (%s)", before, fresh["sim_minute"], fresh.get("ts"))
    return moved


def _resolve_pending(client: SimClient, store: Store, server: dict | None = None) -> bool:
    """Return True when the in-flight advance actually moved simulated time."""
    pending = store.pending_advance_from_minute
    if pending is None:
        return False
    server = server or client.state()
    store.ensure_same_world(server)
    changed: list[str] = []
    if store.last_event_id < int(server["last_event_id"]):
        changed = sync_all(client, store)
        server = client.state()
    moved = int(server["sim_minute"]) > pending
    if not moved:
        store.clear_pending_advance()
        store.audit(
            "advance_not_landed",
            {"from_minute": pending, "sim_minute": server["sim_minute"]},
            sim_ts=server.get("ts"),
        )
        log.info("advance from %s did not land; sim_minute is still %s", pending, server["sim_minute"])
        return False
    store.adopt_clock(server)
    store.audit(
        "advance_reconciled",
        {"from_minute": pending, "sim_minute": server["sim_minute"]},
        sim_ts=server.get("ts"),
    )
    if changed:
        _refresh_workflows(client, store, only=set(changed))
    log.info("reconciled advance %s -> %s without sending it again", pending, server["sim_minute"])
    return True


def _refresh_workflows(client: SimClient, store: Store, only: set[str] | None = None) -> None:
    for document in client.workflows():
        workflow_id = document["workflow_id"]
        if only is not None and workflow_id not in only:
            continue
        store.save_workflow(document)
        current = client.get_config(workflow_id)
        store.save_config_snapshot(
            workflow_id=current["workflow_id"],
            version=int(current["version"]),
            actor=current.get("actor"),
            applied_at=current.get("applied_at"),
            config=current["config"],
            source="fetch",
        )
