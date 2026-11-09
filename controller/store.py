"""SQLite state for the controller.

One file per scenario. A chunk is committed together with the aggregates it
produces, so a crash mid-chunk replays that chunk and a committed chunk is
never applied twice.

Pickup (connected / dials), engagement (engaged / connected), and business
outcome (leads concluded / leads uploaded) are stored as separate counts.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .settings import SIM_DAY_ZERO


SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    scenario TEXT NOT NULL,
    last_chunk INTEGER NOT NULL DEFAULT -1,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    sim_minute INTEGER,
    sim_ts TEXT,
    pending_advance_from_minute INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    events INTEGER NOT NULL,
    first_event_id INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL,
    until_ts TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_counts (
    kind TEXT NOT NULL,
    event_type TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (kind, event_type)
);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    current_version INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    actor TEXT,
    applied_at TEXT,
    config_json TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS config_changes (
    event_id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    actor TEXT NOT NULL,
    changed_dims_json TEXT NOT NULL,
    proposal_id TEXT,
    approval_id TEXT,
    authoritative INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL,
    tenant_id TEXT,
    leads INTEGER NOT NULL,
    config_version_at_creation INTEGER,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    lang TEXT NOT NULL,
    city_tier TEXT NOT NULL,
    age_band TEXT NOT NULL,
    value_band TEXT NOT NULL,
    ts TEXT NOT NULL,
    concluded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS leads_by_person ON leads(workflow_id, person_id);
CREATE INDEX IF NOT EXISTS leads_by_campaign ON leads(workflow_id, campaign_id);

CREATE TABLE IF NOT EXISTS call_agg (
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    call_date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    campaign_day_index INTEGER NOT NULL,
    lang TEXT NOT NULL,
    city_tier TEXT NOT NULL,
    age_band TEXT NOT NULL,
    value_band TEXT NOT NULL,
    welcome_message TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    halluc_flags INTEGER NOT NULL,
    concluded_interested INTEGER NOT NULL,
    concluded_not_interested INTEGER NOT NULL,
    no_answer INTEGER NOT NULL,
    voicemail INTEGER NOT NULL,
    short_hangup INTEGER NOT NULL,
    PRIMARY KEY (
        workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour, campaign_day_index,
        lang, city_tier, age_band, value_band, welcome_message, voice, caller_id, prompt_version
    )
);
CREATE TABLE IF NOT EXISTS kpi_totals (
    workflow_id TEXT PRIMARY KEY,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    leads_uploaded INTEGER NOT NULL DEFAULT 0,
    leads_concluded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS call_group_totals (
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    grain TEXT NOT NULL,
    group_key TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, kind, grain, group_key)
);

CREATE TABLE IF NOT EXISTS lead_group_totals (
    workflow_id TEXT NOT NULL,
    grain TEXT NOT NULL,
    group_key TEXT NOT NULL,
    uploaded INTEGER NOT NULL,
    concluded INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, grain, group_key)
);

CREATE TABLE IF NOT EXISTS controller_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals_seen (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    base_version INTEGER,
    changed_dims_json TEXT,
    reasons_json TEXT,
    event_id INTEGER,
    ts TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    proposed_config_json TEXT NOT NULL,
    change_summary TEXT,
    diff_json TEXT,
    affected_leads INTEGER,
    dry_run_json TEXT,
    impact_score REAL,
    impact_calculation_json TEXT NOT NULL,
    rationale TEXT,
    evidence_json TEXT NOT NULL,
    approval_id TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    route TEXT,
    expires_at TEXT,
    policy_json TEXT,
    verification_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS governance_opportunities (
    opportunity_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    sim_ts TEXT,
    evidence_json TEXT NOT NULL,
    actionable INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    sim_ts TEXT,
    workflow_id TEXT,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    sim_minute INTEGER,
    sim_ts TEXT,
    last_event_id INTEGER,
    events_behind INTEGER,
    credits_remaining REAL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cohort_definitions (
    workflow_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    where_json TEXT NOT NULL,
    support_leads INTEGER NOT NULL,
    support_dials INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_period TEXT NOT NULL,
    selection_evidence_json TEXT,
    built_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, cohort_id)
);

CREATE TABLE IF NOT EXISTS baselines (
    workflow_id TEXT NOT NULL,
    period TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    leads_uploaded INTEGER NOT NULL,
    leads_concluded INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, period)
);

CREATE TABLE IF NOT EXISTS supervisor_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER,
    sim_ts TEXT,
    created_at TEXT NOT NULL,
    document_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    campaign_id TEXT,
    cohort_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    primary_metric TEXT NOT NULL,
    guardrail_metric TEXT NOT NULL,
    guardrail_max_drop REAL NOT NULL,
    control_value TEXT NOT NULL,
    treatment_value TEXT NOT NULL,
    baseline_rate REAL NOT NULL,
    minimum_detectable_effect REAL NOT NULL,
    alpha REAL NOT NULL,
    power REAL NOT NULL,
    allocation_control REAL NOT NULL,
    allocation_treatment REAL NOT NULL,
    required_control INTEGER NOT NULL,
    required_treatment INTEGER NOT NULL,
    sample_unit TEXT NOT NULL,
    status TEXT NOT NULL,
    executable INTEGER NOT NULL,
    exposure_verified INTEGER NOT NULL,
    causal INTEGER NOT NULL,
    base_version INTEGER NOT NULL,
    proposed_config_json TEXT NOT NULL,
    execution_limit TEXT NOT NULL,
    latest_decision TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS experiments_by_workflow ON experiments(workflow_id, status);

CREATE TABLE IF NOT EXISTS experiment_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT,
    workflow_id TEXT NOT NULL,
    source TEXT NOT NULL,
    dimension TEXT NOT NULL,
    current_value TEXT,
    proposed_value TEXT,
    accepted INTEGER NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    experiment_id TEXT,
    kind TEXT NOT NULL,
    eligible INTEGER,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    causal INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS experiment_decisions_by_workflow ON experiment_decisions(workflow_id, kind);

CREATE TABLE IF NOT EXISTS experiment_assignments (
    experiment_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    reason TEXT NOT NULL,
    planned INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, lead_id)
);

CREATE TABLE IF NOT EXISTS experiment_observations (
    experiment_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    leads_uploaded INTEGER,
    leads_concluded INTEGER,
    exposure_verified INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, arm)
);

CREATE TABLE IF NOT EXISTS experiment_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    causal INTEGER NOT NULL,
    confirmatory INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_CALL_AGG_DDL = """
CREATE TABLE call_agg (
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    call_date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    campaign_day_index INTEGER NOT NULL,
    lang TEXT NOT NULL,
    city_tier TEXT NOT NULL,
    age_band TEXT NOT NULL,
    value_band TEXT NOT NULL,
    welcome_message TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    halluc_flags INTEGER NOT NULL,
    concluded_interested INTEGER NOT NULL,
    concluded_not_interested INTEGER NOT NULL,
    no_answer INTEGER NOT NULL,
    voicemail INTEGER NOT NULL,
    short_hangup INTEGER NOT NULL,
    PRIMARY KEY (
        workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour, campaign_day_index,
        lang, city_tier, age_band, value_band, welcome_message, voice, caller_id, prompt_version
    )
)
"""


class WorldReset(RuntimeError):
    """The simulator world is not the one this database checkpointed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


_ROLLUP_READY = "call_rollups"
_CALL_GROUP_COLUMNS = (
    "campaign_id",
    "config_version",
    "cohort_id",
    "call_date",
    "hour",
    "campaign_day_index",
    "lang",
    "city_tier",
    "age_band",
    "value_band",
    "welcome_message",
    "voice",
    "caller_id",
    "prompt_version",
    "kind",
)


def _lead_period(ts: str) -> str:
    return "history" if ts[:10] < SIM_DAY_ZERO else "live"


def pack_key(parts: tuple[str, ...]) -> str:
    """Length-prefix each part so a value cannot be split into another value."""
    return "".join(f"{len(part)}:{part}" for part in parts)


def unpack_key(packed: str, count: int) -> tuple[str, ...]:
    parts: list[str] = []
    index = 0
    for _ in range(count):
        colon = packed.find(":", index)
        if colon < 0:
            raise ValueError("grouped key is truncated")
        length = int(packed[index:colon])
        start = colon + 1
        end = start + length
        if end > len(packed):
            raise ValueError("grouped key is truncated")
        parts.append(packed[start:end])
        index = end
    if index != len(packed):
        raise ValueError("grouped key has trailing bytes")
    return tuple(parts)


def _add_counted(totals: dict, key: tuple, amounts: tuple[int, ...]) -> None:
    bucket = totals.get(key)
    if bucket is None:
        totals[key] = list(amounts)
        return
    for index, amount in enumerate(amounts):
        bucket[index] += amount


def _call_rollup_deltas(agg: dict[tuple, list[int]]) -> dict[tuple, list[int]]:
    """Fold fine call cells into the grains the snapshot and governance read.

    Each cell is one call_agg key. The grains drop the columns a reader does
    not group by, so a chunk writes one row per distinct group, not per call.
    """
    deltas: dict[tuple, list[int]] = {}
    for key, cell in agg.items():
        dials, connected, engaged = int(cell[0]), int(cell[1]), int(cell[2])
        if dials == 0 and connected == 0 and engaged == 0:
            continue
        workflow, kind = key[0], key[1]
        amounts = (dials, connected, engaged)
        _add_counted(deltas, (workflow, kind, "_all", ""), amounts)
        values = (
            key[2],
            str(key[3]),
            key[4],
            key[5],
            str(key[6]),
            str(key[7]),
            key[8],
            key[9],
            key[10],
            key[11],
            key[12],
            key[13],
            key[14],
            key[15],
            kind,
        )
        for grain, value in zip(_CALL_GROUP_COLUMNS, values):
            _add_counted(deltas, (workflow, kind, grain, value), amounts)
        features = pack_key((key[8], key[9], key[10], key[11]))
        _add_counted(deltas, (workflow, kind, "features", features), amounts)
    return deltas


def _remember_lead(
    totals: dict[tuple, list[int]],
    workflow_id: str,
    ts: str,
    campaign_id: str,
    features: tuple[str, str, str, str],
    *,
    uploaded: int,
    concluded: int,
) -> None:
    if uploaded == 0 and concluded == 0:
        return
    period = _lead_period(ts)
    amounts = (uploaded, concluded)
    _add_counted(totals, (workflow_id, "period", period), amounts)
    _add_counted(totals, (workflow_id, "day", ts[:10]), amounts)
    _add_counted(totals, (workflow_id, "campaign", campaign_id), amounts)
    for name, value in zip(("lang", "city_tier", "age_band", "value_band"), features):
        _add_counted(totals, (workflow_id, "feature", pack_key((period, name, value))), amounts)


class Store:
    def __init__(self, path: Path, scenario: str):
        self.path = Path(path)
        self.scenario = scenario
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._tx = False
        try:
            self._migrate_call_agg()
            self._migrate_cohort_definitions()
            self._migrate_experiment_tables()
            self._migrate_governance()
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS call_agg_by_workflow ON call_agg(workflow_id, kind, call_date)"
            )
            self._ensure_checkpoint()
            self._ensure_group_totals()
        except Exception:
            self.conn.close()
            raise

    @classmethod
    def open(cls, state_dir: str | Path, scenario: str) -> Store:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in scenario)
        return cls(Path(state_dir) / f"{safe}.sqlite", scenario)

    def close(self) -> None:
        self.conn.close()

    def _migrate_call_agg(self) -> None:
        """Bring a Phase 1 aggregate forward without dropping it from history or live.

        Phase 1 rows have ``call_date`` and no ``kind``. A date before documented
        D0 becomes ``history``; D0 onward becomes ``events``. A previous migration
        that stored ``kind='legacy'`` is reclassified the same way. An interrupted
        copy leaves ``call_agg_v1`` beside the new table and is resumed only when
        the two copies agree or the new table is still empty.
        """
        if self._table_exists("call_agg_v1"):
            self._recover_call_agg_v1()
        elif not self._call_agg_is_current():
            self._rewrite_phase1_call_agg()
        self._reclassify_legacy_kind()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _call_agg_is_current(self) -> bool:
        cols = [row[1] for row in self.conn.execute("PRAGMA table_info(call_agg)")]
        return "campaign_id" in cols and "kind" in cols

    def _agg_signature(self, table: str) -> tuple[int, int, int, int]:
        row = self.conn.execute(
            f"""SELECT COUNT(*) AS n,
                       COALESCE(SUM(dials), 0) AS dials,
                       COALESCE(SUM(connected), 0) AS connected,
                       COALESCE(SUM(engaged), 0) AS engaged
                FROM {table}"""
        ).fetchone()
        return (int(row["n"]), int(row["dials"]), int(row["connected"]), int(row["engaged"]))

    def _copy_v1_into_call_agg(self) -> None:
        self.conn.execute(
            """INSERT INTO call_agg (
                   workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
                   campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
                   voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
                   concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
               )
               SELECT workflow_id,
                      CASE WHEN call_date < ? THEN 'history' ELSE 'events' END,
                      '',
                      config_version, cohort_id, call_date, hour, campaign_day_index,
                      lang, city_tier, age_band, value_band, welcome_message, voice, caller_id,
                      prompt_version, dials, connected, engaged, halluc_flags,
                      concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
               FROM call_agg_v1""",
            (SIM_DAY_ZERO,),
        )

    def _rewrite_phase1_call_agg(self) -> None:
        with self.transaction():
            self.conn.execute("ALTER TABLE call_agg RENAME TO call_agg_v1")
            self.conn.execute(_CALL_AGG_DDL)
            self._copy_v1_into_call_agg()
            self.conn.execute("DROP TABLE call_agg_v1")

    def _recover_call_agg_v1(self) -> None:
        if not self._table_exists("call_agg"):
            self.conn.execute(_CALL_AGG_DDL)
        new_signature = self._agg_signature("call_agg")
        old_signature = self._agg_signature("call_agg_v1")
        empty = (0, 0, 0, 0)
        with self.transaction():
            if new_signature == empty and old_signature != empty:
                self._copy_v1_into_call_agg()
            elif new_signature != old_signature and new_signature != empty and old_signature != empty:
                raise RuntimeError(
                    "call_agg migration stopped between the old and new tables with different totals; "
                    "refusing to drop either copy"
                )
            self.conn.execute("DROP TABLE IF EXISTS call_agg_v1")

    def _reclassify_legacy_kind(self) -> None:
        """Move kind='legacy' rows onto history or events without double-counting."""
        if not self._call_agg_is_current():
            return
        pending = self.conn.execute(
            "SELECT 1 FROM call_agg WHERE kind = 'legacy' LIMIT 1"
        ).fetchone()
        if pending is None:
            return
        with self.transaction():
            self.conn.execute(
                """INSERT INTO call_agg (
                       workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
                       campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
                       voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
                       concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
                   )
                   SELECT workflow_id,
                          CASE WHEN call_date < ? THEN 'history' ELSE 'events' END,
                          campaign_id, config_version, cohort_id, call_date, hour, campaign_day_index,
                          lang, city_tier, age_band, value_band, welcome_message, voice, caller_id,
                          prompt_version,
                          SUM(dials), SUM(connected), SUM(engaged), SUM(halluc_flags),
                          SUM(concluded_interested), SUM(concluded_not_interested),
                          SUM(no_answer), SUM(voicemail), SUM(short_hangup)
                   FROM call_agg
                   WHERE kind = 'legacy'
                   GROUP BY workflow_id, campaign_id, config_version, cohort_id, call_date, hour,
                            campaign_day_index, lang, city_tier, age_band, value_band,
                            welcome_message, voice, caller_id, prompt_version
                   ON CONFLICT(
                       workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
                       campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
                       voice, caller_id, prompt_version
                   ) DO UPDATE SET
                       dials = call_agg.dials + excluded.dials,
                       connected = call_agg.connected + excluded.connected,
                       engaged = call_agg.engaged + excluded.engaged,
                       halluc_flags = call_agg.halluc_flags + excluded.halluc_flags,
                       concluded_interested = call_agg.concluded_interested + excluded.concluded_interested,
                       concluded_not_interested = call_agg.concluded_not_interested + excluded.concluded_not_interested,
                       no_answer = call_agg.no_answer + excluded.no_answer,
                       voicemail = call_agg.voicemail + excluded.voicemail,
                       short_hangup = call_agg.short_hangup + excluded.short_hangup""",
                (SIM_DAY_ZERO,),
            )
            self.conn.execute("DELETE FROM call_agg WHERE kind = 'legacy'")

    def _migrate_cohort_definitions(self) -> None:
        cols = [row[1] for row in self.conn.execute("PRAGMA table_info(cohort_definitions)")]
        if "selection_evidence_json" not in cols:
            self.conn.execute("ALTER TABLE cohort_definitions ADD COLUMN selection_evidence_json TEXT")

    def _migrate_governance(self) -> None:
        """Add proposal governance columns without rewriting existing proposals."""
        self._add_column("proposals", "route", "TEXT")
        self._add_column("proposals", "expires_at", "TEXT")
        self._add_column("proposals", "policy_json", "TEXT")
        self._add_column("proposals", "verification_json", "TEXT")

    def _migrate_experiment_tables(self) -> None:
        """Add selection and confirmatory columns without rewriting experiment rows.

        Databases created before those columns keep every existing experiment,
        candidate, and evaluation. New rows record which candidate was selected
        and which evaluation spent the single confirmatory look.
        """
        self._add_column("experiment_candidates", "selected", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("experiment_evaluations", "confirmatory", "INTEGER NOT NULL DEFAULT 0")

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        if not self._table_exists(table):
            return
        cols = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _ensure_checkpoint(self) -> None:
        row = self.conn.execute("SELECT scenario FROM checkpoint WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO checkpoint (id, scenario, updated_at) VALUES (1, ?, ?)",
                (self.scenario, _now()),
            )
        elif row["scenario"] != self.scenario:
            raise WorldReset(
                f"database scenario is {row['scenario']}, server scenario is {self.scenario}"
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._tx:
            yield
            return
        self._tx = True
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._tx = False

    # --- checkpoint ---

    def _checkpoint(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM checkpoint WHERE id = 1").fetchone()
        assert row is not None
        return row

    @property
    def last_chunk(self) -> int:
        return int(self._checkpoint()["last_chunk"])

    @property
    def last_event_id(self) -> int:
        return int(self._checkpoint()["last_event_id"])

    @property
    def sim_minute(self) -> int | None:
        value = self._checkpoint()["sim_minute"]
        return None if value is None else int(value)

    @property
    def sim_ts(self) -> str | None:
        return self._checkpoint()["sim_ts"]

    @property
    def pending_advance_from_minute(self) -> int | None:
        value = self._checkpoint()["pending_advance_from_minute"]
        return None if value is None else int(value)

    def ensure_same_world(self, server: dict) -> None:
        if server.get("scenario") != self.scenario:
            raise WorldReset(
                f"database scenario is {self.scenario}, server scenario is {server.get('scenario')}"
            )
        if self.last_event_id > int(server["last_event_id"]):
            raise WorldReset(
                "server last_event_id is behind this checkpoint; the simulator world was recreated"
            )
        server_minute = int(server["sim_minute"])
        if self.sim_minute is not None and server_minute < self.sim_minute:
            raise WorldReset(
                "server sim_minute is behind this checkpoint; the simulator world was recreated"
            )

    def mark_pending_advance(self, from_minute: int) -> None:
        with self.transaction():
            self.conn.execute(
                """UPDATE checkpoint
                   SET pending_advance_from_minute = ?, updated_at = ?
                   WHERE id = 1""",
                (from_minute, _now()),
            )

    def clear_pending_advance(self) -> None:
        with self.transaction():
            self.conn.execute(
                """UPDATE checkpoint
                   SET pending_advance_from_minute = NULL, updated_at = ?
                   WHERE id = 1""",
                (_now(),),
            )

    def adopt_clock(self, server: dict) -> None:
        """Record the server clock and drop the in-flight advance marker."""
        with self.transaction():
            self.conn.execute(
                """UPDATE checkpoint
                   SET sim_minute = ?, sim_ts = ?, pending_advance_from_minute = NULL, updated_at = ?
                   WHERE id = 1""",
                (int(server["sim_minute"]), server.get("ts"), _now()),
            )

    def chunk_applied(self, chunk: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM chunks WHERE chunk = ?", (chunk,)).fetchone()
        return row is not None

    # --- reference data ---

    def save_workflow(self, document: dict) -> None:
        with self.transaction():
            self.conn.execute(
                """INSERT INTO workflows (workflow_id, tenant_id, document_json, current_version, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id) DO UPDATE SET
                     tenant_id = excluded.tenant_id,
                     document_json = excluded.document_json,
                     current_version = excluded.current_version,
                     updated_at = excluded.updated_at""",
                (
                    document["workflow_id"],
                    document.get("tenant_id") or "",
                    _dumps(document),
                    document.get("current_version"),
                    _now(),
                ),
            )

    def save_config_snapshot(
        self,
        workflow_id: str,
        version: int,
        actor: str | None,
        applied_at: str | None,
        config: dict,
        source: str,
    ) -> None:
        with self.transaction():
            self.conn.execute(
                """INSERT INTO config_snapshots
                   (workflow_id, version, actor, applied_at, config_json, source, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id, version) DO UPDATE SET
                     actor = excluded.actor,
                     applied_at = excluded.applied_at,
                     config_json = excluded.config_json,
                     source = excluded.source,
                     observed_at = excluded.observed_at""",
                (workflow_id, version, actor, applied_at, _dumps(config), source, _now()),
            )

    def save_proposal(self, proposal: dict) -> None:
        """Persist the exact config, evidence, impact math, status, and outcome."""
        required = (
            "proposal_id",
            "workflow_id",
            "base_version",
            "proposed_config",
            "impact_calculation",
            "evidence",
            "status",
        )
        missing = [key for key in required if key not in proposal]
        if missing:
            raise ValueError(f"proposal missing {missing}")
        now = _now()
        with self.transaction():
            self.conn.execute(
                """INSERT INTO proposals (
                       proposal_id, workflow_id, base_version, proposed_config_json,
                       change_summary, diff_json, affected_leads, dry_run_json, impact_score,
                       impact_calculation_json, rationale, evidence_json, approval_id, status,
                       outcome, route, expires_at, policy_json, verification_json,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(proposal_id) DO UPDATE SET
                     workflow_id = excluded.workflow_id,
                     base_version = excluded.base_version,
                     proposed_config_json = excluded.proposed_config_json,
                     change_summary = excluded.change_summary,
                     diff_json = excluded.diff_json,
                     affected_leads = excluded.affected_leads,
                     dry_run_json = excluded.dry_run_json,
                     impact_score = excluded.impact_score,
                     impact_calculation_json = excluded.impact_calculation_json,
                     rationale = excluded.rationale,
                     evidence_json = excluded.evidence_json,
                     approval_id = excluded.approval_id,
                     status = excluded.status,
                     outcome = excluded.outcome,
                     route = excluded.route,
                     expires_at = excluded.expires_at,
                     policy_json = excluded.policy_json,
                     verification_json = COALESCE(excluded.verification_json, proposals.verification_json),
                     updated_at = excluded.updated_at""",
                (
                    proposal["proposal_id"],
                    proposal["workflow_id"],
                    int(proposal["base_version"]),
                    _dumps(proposal["proposed_config"]),
                    proposal.get("change_summary"),
                    _dumps(proposal["diff"]) if "diff" in proposal else None,
                    proposal.get("affected_leads"),
                    _dumps(proposal["dry_run"]) if "dry_run" in proposal else None,
                    proposal.get("impact_score"),
                    _dumps(proposal["impact_calculation"]),
                    proposal.get("rationale"),
                    _dumps(proposal["evidence"]),
                    proposal.get("approval_id"),
                    proposal["status"],
                    proposal.get("outcome"),
                    proposal.get("route"),
                    proposal.get("expires_at"),
                    _dumps(proposal["policy"]) if proposal.get("policy") is not None else None,
                    _dumps(proposal["verification"]) if proposal.get("verification") is not None else None,
                    proposal.get("created_at") or now,
                    now,
                ),
            )

    def get_proposal(self, proposal_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key, target in (
            ("proposed_config_json", "proposed_config"),
            ("diff_json", "diff"),
            ("dry_run_json", "dry_run"),
            ("impact_calculation_json", "impact_calculation"),
            ("evidence_json", "evidence"),
            ("policy_json", "policy"),
            ("verification_json", "verification"),
        ):
            raw = out.pop(key, None)
            out[target] = json.loads(raw) if raw else None
        return out

    def proposals_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT proposal_id FROM proposals WHERE status IN ({marks}) ORDER BY created_at, proposal_id",
            statuses,
        ).fetchall()
        found = []
        for row in rows:
            proposal = self.get_proposal(row["proposal_id"])
            if proposal is not None:
                found.append(proposal)
        return found

    def open_proposal_for(self, workflow_id: str) -> dict | None:
        terminal = (
            "policy_rejected",
            "rejected",
            "expired",
            "verified",
            "failed",
            "lapsed",
            "rolled_back",
        )
        marks = ",".join("?" for _ in terminal)
        row = self.conn.execute(
            f"""SELECT proposal_id FROM proposals
                WHERE workflow_id = ? AND status NOT IN ({marks})
                ORDER BY created_at DESC LIMIT 1""",
            (workflow_id, *terminal),
        ).fetchone()
        if row is None:
            return None
        return self.get_proposal(row["proposal_id"])

    def get_opportunity(self, opportunity_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM governance_opportunities WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["actionable"] = bool(item["actionable"])
        return item

    def record_opportunity(self, opportunity: dict) -> None:
        """Insert the first observation and refresh later evidence in place.

        ``detected_at`` and the first ``sim_ts`` stay on conflict. Existing
        databases already have this table, so no column migration is required.
        """
        with self.transaction():
            self.conn.execute(
                """INSERT INTO governance_opportunities (
                       opportunity_id, workflow_id, detected_at, sim_ts, evidence_json,
                       actionable, reason
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(opportunity_id) DO UPDATE SET
                     evidence_json = excluded.evidence_json,
                     actionable = excluded.actionable,
                     reason = excluded.reason""",
                (
                    opportunity["opportunity_id"],
                    opportunity["workflow_id"],
                    opportunity.get("detected_at") or _now(),
                    opportunity.get("sim_ts"),
                    _dumps(opportunity["evidence"]),
                    1 if opportunity.get("actionable") else 0,
                    opportunity["reason"],
                ),
            )

    def record_attempt(self, proposal_id: str, workflow_id: str, kind: str, outcome: str, detail: dict) -> int:
        with self.transaction():
            cur = self.conn.execute(
                """INSERT INTO application_attempts (
                       proposal_id, workflow_id, kind, outcome, detail_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (proposal_id, workflow_id, kind, outcome, _dumps(detail), _now()),
            )
            return int(cur.lastrowid)

    def attempts_for(self, proposal_id: str, kind: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM application_attempts
               WHERE proposal_id = ? AND kind = ? ORDER BY id""",
            (proposal_id, kind),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            out.append(item)
        return out

    def audit(self, action: str, detail: dict, workflow_id: str | None = None, sim_ts: str | None = None) -> None:
        with self.transaction():
            self.conn.execute(
                """INSERT INTO audit_log (ts, sim_ts, workflow_id, action, detail_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (_now(), sim_ts, workflow_id, action, _dumps(detail)),
            )

    def has_audit_detail(self, action: str, workflow_id: str, identity: dict) -> bool:
        """True when this workflow already has an audit whose detail matches ``identity``.

        Identity is a subset of the stored detail. A new supervisor version, or a
        different outcome, does not match an older row.
        """
        rows = self.conn.execute(
            "SELECT detail_json FROM audit_log WHERE action = ? AND workflow_id = ?",
            (action, workflow_id),
        ).fetchall()
        for row in rows:
            detail = json.loads(row["detail_json"])
            if isinstance(detail, dict) and all(detail.get(key) == value for key, value in identity.items()):
                return True
        return False

    def record_metric(self, server: dict, detail: dict | None = None) -> None:
        behind = int(server["last_event_id"]) - self.last_event_id
        with self.transaction():
            self.conn.execute(
                """INSERT INTO metrics
                   (ts, sim_minute, sim_ts, last_event_id, events_behind, credits_remaining, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(),
                    int(server["sim_minute"]),
                    server.get("ts"),
                    self.last_event_id,
                    behind,
                    server.get("credits_remaining"),
                    _dumps(detail or {}),
                ),
            )

    # --- grouped totals kept with ingest ---

    def _ensure_group_totals(self) -> None:
        """Build grouped totals once for a database that already holds events.

        A new database is marked ready while it is empty. Later chunks add the
        same deltas they add to call_agg and leads. An old database is scanned
        once, in this transaction, and is not scanned again on later opens.
        """
        row = self.conn.execute(
            "SELECT value FROM controller_meta WHERE key = ?",
            (_ROLLUP_READY,),
        ).fetchone()
        if row is not None:
            return
        with self.transaction():
            self._rebuild_group_totals()
            self.conn.execute(
                "INSERT INTO controller_meta (key, value) VALUES (?, '1')",
                (_ROLLUP_READY,),
            )

    def _rebuild_group_totals(self) -> None:
        """Replace grouped totals from call_agg and leads. Exact sums, one scan per grain."""
        self.conn.execute("DELETE FROM call_group_totals")
        self.conn.execute("DELETE FROM lead_group_totals")
        self.conn.execute(
            """INSERT INTO call_group_totals (workflow_id, kind, grain, group_key, dials, connected, engaged)
               SELECT workflow_id, kind, '_all', '',
                      SUM(dials), SUM(connected), SUM(engaged)
               FROM call_agg GROUP BY workflow_id, kind"""
        )
        for grain in _CALL_GROUP_COLUMNS:
            if grain == "kind":
                self.conn.execute(
                    """INSERT INTO call_group_totals (workflow_id, kind, grain, group_key, dials, connected, engaged)
                       SELECT workflow_id, kind, 'kind', kind,
                              SUM(dials), SUM(connected), SUM(engaged)
                       FROM call_agg GROUP BY workflow_id, kind"""
                )
                continue
            self.conn.execute(
                f"""INSERT INTO call_group_totals (workflow_id, kind, grain, group_key, dials, connected, engaged)
                    SELECT workflow_id, kind, ?, CAST({grain} AS TEXT),
                           SUM(dials), SUM(connected), SUM(engaged)
                    FROM call_agg GROUP BY workflow_id, kind, {grain}""",
                (grain,),
            )
        self.conn.execute(
            """INSERT INTO call_group_totals (workflow_id, kind, grain, group_key, dials, connected, engaged)
               SELECT workflow_id, kind, 'features',
                      length(lang) || ':' || lang
                      || length(city_tier) || ':' || city_tier
                      || length(age_band) || ':' || age_band
                      || length(value_band) || ':' || value_band,
                      SUM(dials), SUM(connected), SUM(engaged)
               FROM call_agg
               GROUP BY workflow_id, kind, lang, city_tier, age_band, value_band"""
        )
        self.conn.execute(
            """INSERT INTO lead_group_totals (workflow_id, grain, group_key, uploaded, concluded)
               SELECT workflow_id, 'period',
                      CASE WHEN substr(ts, 1, 10) < ? THEN 'history' ELSE 'live' END,
                      COUNT(*), COALESCE(SUM(concluded), 0)
               FROM leads
               GROUP BY workflow_id, 3""",
            (SIM_DAY_ZERO,),
        )
        self.conn.execute(
            """INSERT INTO lead_group_totals (workflow_id, grain, group_key, uploaded, concluded)
               SELECT workflow_id, 'day', substr(ts, 1, 10),
                      COUNT(*), COALESCE(SUM(concluded), 0)
               FROM leads GROUP BY workflow_id, 3"""
        )
        self.conn.execute(
            """INSERT INTO lead_group_totals (workflow_id, grain, group_key, uploaded, concluded)
               SELECT workflow_id, 'campaign', campaign_id,
                      COUNT(*), COALESCE(SUM(concluded), 0)
               FROM leads GROUP BY workflow_id, campaign_id"""
        )
        for feature in ("lang", "city_tier", "age_band", "value_band"):
            self.conn.execute(
                f"""INSERT INTO lead_group_totals (workflow_id, grain, group_key, uploaded, concluded)
                    SELECT workflow_id, 'feature',
                           length(period) || ':' || period
                           || length(?) || ':' || ?
                           || length({feature}) || ':' || {feature},
                           COUNT(*), COALESCE(SUM(concluded), 0)
                    FROM (
                        SELECT workflow_id, {feature}, concluded,
                               CASE WHEN substr(ts, 1, 10) < ? THEN 'history' ELSE 'live' END AS period
                        FROM leads
                    )
                    GROUP BY workflow_id, 3""",
                (feature, feature, SIM_DAY_ZERO),
            )

    def _flush_call_rollups(self, deltas: dict[tuple, list[int]]) -> None:
        if not deltas:
            return
        self.conn.executemany(
            """INSERT INTO call_group_totals (workflow_id, kind, grain, group_key, dials, connected, engaged)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id, kind, grain, group_key) DO UPDATE SET
                 dials = call_group_totals.dials + excluded.dials,
                 connected = call_group_totals.connected + excluded.connected,
                 engaged = call_group_totals.engaged + excluded.engaged""",
            [
                (workflow_id, kind, grain, group_key, dials, connected, engaged)
                for (workflow_id, kind, grain, group_key), (dials, connected, engaged) in deltas.items()
            ],
        )

    def _flush_lead_rollups(self, deltas: dict[tuple, list[int]]) -> None:
        if not deltas:
            return
        self.conn.executemany(
            """INSERT INTO lead_group_totals (workflow_id, grain, group_key, uploaded, concluded)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id, grain, group_key) DO UPDATE SET
                 uploaded = lead_group_totals.uploaded + excluded.uploaded,
                 concluded = lead_group_totals.concluded + excluded.concluded""",
            [
                (workflow_id, grain, group_key, uploaded, concluded)
                for (workflow_id, grain, group_key), (uploaded, concluded) in deltas.items()
            ],
        )

    # --- ingestion ---

    def apply_chunk(self, meta: dict, events, kind: str) -> list[str]:
        """Apply one chunk. Returns workflow ids whose config changed.

        Already-applied chunks are a no-op. The write is one transaction, so a
        crash rolls the raw rows and the grouped totals back together.
        ``events`` may be a generator; it is not stored.
        """
        chunk_id = int(meta["chunk"])
        if self.chunk_applied(chunk_id):
            return []

        changed: list[str] = []
        new_leads: list[tuple] = []
        concluded: list[str] = []
        missing_leads: list[str] = []
        campaigns: list[tuple] = []
        changes: list[tuple] = []
        approvals: list[tuple] = []
        proposal_updates: list[tuple] = []
        agg: dict[tuple, list[int]] = {}
        counts: dict[str, int] = {}
        kpi: dict[str, list[int]] = {}
        feature_cache: dict[str, tuple[str, str, str, str]] = {}
        last_kept = self.last_event_id
        previous_id: int | None = None
        n_events = 0
        first_id: int | None = None
        last_id: int | None = None
        n_kept = 0
        first_kept: int | None = None
        last_kept_seen: int | None = None

        for ev in events:
            n_events += 1
            event_id = int(ev["event_id"])
            if previous_id is None:
                first_id = event_id
            elif event_id != previous_id + 1:
                raise ValueError(
                    f"chunk {chunk_id} event ids are not contiguous "
                    f"between {previous_id} and {event_id}"
                )
            previous_id = event_id
            last_id = event_id
            if event_id <= last_kept:
                continue
            n_kept += 1
            if first_kept is None:
                first_kept = event_id
            last_kept_seen = event_id
            etype = ev["type"]
            counts[etype] = counts.get(etype, 0) + 1
            if etype == "lead":
                features = (
                    str(ev["features"]["lang"]),
                    str(ev["features"]["city_tier"]),
                    str(ev["features"]["age_band"]),
                    str(ev["features"]["value_band"]),
                )
                feature_cache[ev["lead_id"]] = features
                new_leads.append(
                    (
                        ev["lead_id"],
                        event_id,
                        ev["workflow_id"],
                        ev["campaign_id"],
                        ev["person_id"],
                        *features,
                        ev["ts"],
                    )
                )
                bucket = kpi.setdefault(ev["workflow_id"], [0, 0, 0, 0, 0])
                bucket[3] += 1
            elif etype == "call":
                features = self._features(ev["lead_id"], feature_cache)
                if features == ("", "", "", ""):
                    missing_leads.append(ev["lead_id"])
                variant = ev["variant"]
                qa = ev.get("qa") or {}
                hour, call_date = _hour_and_date(ev["ts"])
                disposition = ev["disposition"]
                key = (
                    ev["workflow_id"],
                    kind,
                    str(ev.get("campaign_id") or ""),
                    int(ev["config_version"]),
                    str(ev["cohort_id"]),
                    call_date,
                    hour,
                    int(ev["campaign_day_index"]),
                    *features,
                    str(variant["welcome_message"]),
                    str(variant["voice"]),
                    str(variant["caller_id"]),
                    str(variant["prompt_version"]),
                )
                cell = agg.setdefault(key, [0, 0, 0, 0, 0, 0, 0, 0, 0])
                cell[0] += 1
                connected = 1 if ev["connected"] else 0
                engaged = 1 if ev["engaged"] else 0
                cell[1] += connected
                cell[2] += engaged
                cell[3] += 1 if qa.get("hallucination_flag") else 0
                cell[4] += 1 if disposition == "concluded_interested" else 0
                cell[5] += 1 if disposition == "concluded_not_interested" else 0
                cell[6] += 1 if disposition == "no_answer" else 0
                cell[7] += 1 if disposition == "voicemail" else 0
                cell[8] += 1 if disposition == "short_hangup" else 0
                bucket = kpi.setdefault(ev["workflow_id"], [0, 0, 0, 0, 0])
                bucket[0] += 1
                bucket[1] += connected
                bucket[2] += engaged
                if engaged:
                    concluded.append(ev["lead_id"])
            elif etype == "campaign_created":
                campaigns.append(
                    (
                        ev["campaign_id"],
                        event_id,
                        ev["workflow_id"],
                        ev.get("tenant_id"),
                        int(ev["leads"]),
                        ev.get("config_version_at_creation"),
                        ev["ts"],
                    )
                )
            elif etype == "config_changed":
                actor = str(ev["actor"])
                authoritative = 1 if actor == "supervisor" else 0
                changes.append(
                    (
                        event_id,
                        ev["ts"],
                        ev["workflow_id"],
                        int(ev["version"]),
                        actor,
                        _dumps(ev.get("changed_dims") or []),
                        ev.get("proposal_id"),
                        ev.get("approval_id"),
                        authoritative,
                    )
                )
                changed.append(ev["workflow_id"])
                if ev.get("proposal_id"):
                    proposal_updates.append(
                        (
                            "applied",
                            f"config_changed by {actor}",
                            ev.get("approval_id"),
                            ev["proposal_id"],
                        )
                    )
            elif etype == "approval_decided":
                approvals.append(
                    (
                        ev["approval_id"],
                        ev.get("proposal_id"),
                        ev["workflow_id"],
                        ev["status"],
                        ev.get("base_version"),
                        _dumps(ev.get("changed_dims") or []),
                        _dumps(ev.get("reasons") or []),
                        event_id,
                        ev["ts"],
                        _now(),
                    )
                )
                proposal_updates.append(
                    (
                        ev["status"],
                        _dumps(ev.get("reasons") or []),
                        ev["approval_id"],
                        None,
                    )
                )
            else:
                raise ValueError(f"unknown event type {etype}")
        feature_cache.clear()

        if n_events == 0:
            with self.transaction():
                if self.chunk_applied(chunk_id):
                    return []
                self._mark_chunk(meta, kind, 0, 0, 0)
            return []

        call_deltas = _call_rollup_deltas(agg)
        lead_deltas: dict[tuple, list[int]] = {}
        with self.transaction():
            if self.chunk_applied(chunk_id):
                return []
            uploaded_by_wf: dict[str, int] = {}
            for lead in new_leads:
                before = self.conn.total_changes
                self.conn.execute(
                    """INSERT OR IGNORE INTO leads (
                           lead_id, event_id, workflow_id, campaign_id, person_id,
                           lang, city_tier, age_band, value_band, ts
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    lead,
                )
                if self.conn.total_changes == before:
                    uploaded_by_wf[lead[2]] = uploaded_by_wf.get(lead[2], 0) - 1
                else:
                    _remember_lead(
                        lead_deltas,
                        lead[2],
                        lead[9],
                        lead[3],
                        (lead[5], lead[6], lead[7], lead[8]),
                        uploaded=1,
                        concluded=0,
                    )
            concluded_by_wf: dict[str, int] = {}
            for lead_id in concluded:
                row = self.conn.execute(
                    """SELECT workflow_id, campaign_id, lang, city_tier, age_band, value_band, ts
                       FROM leads WHERE lead_id = ?""",
                    (lead_id,),
                ).fetchone()
                if row is None:
                    continue
                cur = self.conn.execute(
                    "UPDATE leads SET concluded = 1 WHERE lead_id = ? AND concluded = 0",
                    (lead_id,),
                )
                if cur.rowcount == 1:
                    concluded_by_wf[row["workflow_id"]] = concluded_by_wf.get(row["workflow_id"], 0) + 1
                    _remember_lead(
                        lead_deltas,
                        row["workflow_id"],
                        row["ts"],
                        row["campaign_id"],
                        (row["lang"], row["city_tier"], row["age_band"], row["value_band"]),
                        uploaded=0,
                        concluded=1,
                    )
            for key, cell in agg.items():
                self.conn.execute(
                    """INSERT INTO call_agg (
                           workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
                           campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
                           voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
                           concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(
                           workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
                           campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
                           voice, caller_id, prompt_version
                       ) DO UPDATE SET
                           dials = call_agg.dials + excluded.dials,
                           connected = call_agg.connected + excluded.connected,
                           engaged = call_agg.engaged + excluded.engaged,
                           halluc_flags = call_agg.halluc_flags + excluded.halluc_flags,
                           concluded_interested = call_agg.concluded_interested + excluded.concluded_interested,
                           concluded_not_interested = call_agg.concluded_not_interested + excluded.concluded_not_interested,
                           no_answer = call_agg.no_answer + excluded.no_answer,
                           voicemail = call_agg.voicemail + excluded.voicemail,
                           short_hangup = call_agg.short_hangup + excluded.short_hangup""",
                    (*key, *cell),
                )
            for workflow_id, bucket in kpi.items():
                uploaded = bucket[3] + uploaded_by_wf.get(workflow_id, 0)
                concluded_n = concluded_by_wf.get(workflow_id, 0)
                self.conn.execute(
                    """INSERT INTO kpi_totals (workflow_id, dials, connected, engaged, leads_uploaded, leads_concluded)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(workflow_id) DO UPDATE SET
                         dials = kpi_totals.dials + excluded.dials,
                         connected = kpi_totals.connected + excluded.connected,
                         engaged = kpi_totals.engaged + excluded.engaged,
                         leads_uploaded = kpi_totals.leads_uploaded + excluded.leads_uploaded,
                         leads_concluded = kpi_totals.leads_concluded + excluded.leads_concluded""",
                    (workflow_id, bucket[0], bucket[1], bucket[2], uploaded, concluded_n),
                )
            self._flush_call_rollups(call_deltas)
            self._flush_lead_rollups(lead_deltas)
            for campaign in campaigns:
                self.conn.execute(
                    """INSERT OR IGNORE INTO campaigns
                       (campaign_id, event_id, workflow_id, tenant_id, leads, config_version_at_creation, ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    campaign,
                )
            for change in changes:
                self.conn.execute(
                    """INSERT OR IGNORE INTO config_changes
                       (event_id, ts, workflow_id, version, actor, changed_dims_json, proposal_id,
                        approval_id, authoritative)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    change,
                )
                if change[8] == 1:
                    self.conn.execute(
                        """INSERT INTO audit_log (ts, sim_ts, workflow_id, action, detail_json)
                           VALUES (?, ?, ?, 'supervisor_change_authoritative', ?)""",
                        (_now(), change[1], change[2], _dumps({
                            "event_id": change[0],
                            "version": change[3],
                            "changed_dims": json.loads(change[5]),
                            "note": "supervisor edit is authoritative; controller will not reverse it",
                        })),
                    )
            for approval in approvals:
                self.conn.execute(
                    """INSERT INTO approvals_seen (
                           approval_id, proposal_id, workflow_id, status, base_version, changed_dims_json,
                           reasons_json, event_id, ts, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(approval_id) DO UPDATE SET
                         status = excluded.status,
                         reasons_json = excluded.reasons_json,
                         event_id = excluded.event_id,
                         ts = excluded.ts,
                         updated_at = excluded.updated_at""",
                    approval,
                )
            now = _now()
            for status, outcome, approval_id, proposal_id in proposal_updates:
                if proposal_id:
                    self.conn.execute(
                        """UPDATE proposals
                           SET status = ?, outcome = ?, approval_id = COALESCE(?, approval_id), updated_at = ?
                           WHERE proposal_id = ?""",
                        (status, outcome, approval_id, now, proposal_id),
                    )
                elif approval_id:
                    self.conn.execute(
                        """UPDATE proposals
                           SET status = ?, outcome = ?, updated_at = ?
                           WHERE approval_id = ?""",
                        (status, outcome, now, approval_id),
                    )
            for lead_id in missing_leads:
                self.conn.execute(
                    """INSERT INTO audit_log (ts, sim_ts, workflow_id, action, detail_json)
                       VALUES (?, NULL, NULL, 'call_missing_lead', ?)""",
                    (_now(), _dumps({"lead_id": lead_id, "chunk": chunk_id})),
                )
            for etype, n in counts.items():
                self.conn.execute(
                    """INSERT INTO ingest_counts (kind, event_type, n) VALUES (?, ?, ?)
                       ON CONFLICT(kind, event_type) DO UPDATE SET n = ingest_counts.n + excluded.n""",
                    (kind, etype, n),
                )
            if n_kept:
                first = int(first_kept)
                last = int(last_kept_seen)
                recorded = n_kept
            else:
                first = int(meta.get("first_event_id") or first_id)
                last = int(meta.get("last_event_id") or last_id)
                recorded = n_events
            self._mark_chunk(meta, kind, recorded, first, last)
        return changed

    def _mark_chunk(self, meta: dict, kind: str, events: int, first: int, last: int) -> None:
        self.conn.execute(
            """INSERT INTO chunks (chunk, kind, events, first_event_id, last_event_id, until_ts, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                int(meta["chunk"]),
                kind,
                events,
                first,
                last,
                meta.get("until_ts"),
                _now(),
            ),
        )
        self.conn.execute(
            """UPDATE checkpoint
               SET last_chunk = ?, last_event_id = ?, updated_at = ?
               WHERE id = 1 AND last_chunk < ?""",
            (int(meta["chunk"]), max(last, self.last_event_id), _now(), int(meta["chunk"])),
        )

    def _features(self, lead_id: str, cache: dict[str, tuple[str, str, str, str]]) -> tuple[str, str, str, str]:
        if lead_id in cache:
            return cache[lead_id]
        row = self.conn.execute(
            "SELECT lang, city_tier, age_band, value_band FROM leads WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        if row is None:
            features = ("", "", "", "")
        else:
            features = (row["lang"], row["city_tier"], row["age_band"], row["value_band"])
        cache[lead_id] = features
        return features

    # --- reads for tests and the phase-1 report ---

    def ingest_counts(self) -> dict[tuple[str, str], int]:
        rows = self.conn.execute("SELECT kind, event_type, n FROM ingest_counts").fetchall()
        return {(row["kind"], row["event_type"]): int(row["n"]) for row in rows}

    def kpis(self, workflow_id: str | None = None) -> dict[str, dict]:
        if workflow_id is None:
            rows = self.conn.execute("SELECT * FROM kpi_totals ORDER BY workflow_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM kpi_totals WHERE workflow_id = ?", (workflow_id,)
            ).fetchall()
        return {row["workflow_id"]: _kpi_row(row) for row in rows}

    def call_totals(self) -> dict[str, int]:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(dials), 0) AS dials,
                      COALESCE(SUM(connected), 0) AS connected,
                      COALESCE(SUM(engaged), 0) AS engaged
               FROM call_agg"""
        ).fetchone()
        return {"dials": int(row["dials"]), "connected": int(row["connected"]), "engaged": int(row["engaged"])}

    def lead_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"])

    def authoritative_changes(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM config_changes WHERE authoritative = 1 ORDER BY event_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def audit_actions(self) -> list[str]:
        rows = self.conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()
        return [row["action"] for row in rows]

    def proposal_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"])

    def workflow_documents(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT document_json FROM workflows ORDER BY workflow_id"
        ).fetchall()
        return [json.loads(row["document_json"]) for row in rows]

    def replace_cohorts(
        self,
        workflow_id: str,
        cohorts: list[dict],
        source_period: str,
        selection_evidence: dict | None = None,
    ) -> None:
        now = _now()
        evidence_json = _dumps(selection_evidence) if selection_evidence else None
        with self.transaction():
            self.conn.execute("DELETE FROM cohort_definitions WHERE workflow_id = ?", (workflow_id,))
            for cohort in cohorts:
                self.conn.execute(
                    """INSERT INTO cohort_definitions (
                           workflow_id, position, cohort_id, where_json, support_leads, support_dials,
                           reason, source_period, selection_evidence_json, built_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workflow_id,
                        int(cohort["position"]),
                        cohort["cohort_id"],
                        _dumps(cohort["where"]),
                        int(cohort["support_leads"]),
                        int(cohort["support_dials"]),
                        cohort["reason"],
                        source_period,
                        evidence_json,
                        now,
                    ),
                )

    def cohort_rows(self, workflow_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT position, cohort_id, where_json, support_leads, support_dials, reason,
                      source_period, selection_evidence_json
               FROM cohort_definitions WHERE workflow_id = ? ORDER BY position, cohort_id""",
            (workflow_id,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["where"] = json.loads(item.pop("where_json"))
            raw = item.pop("selection_evidence_json")
            item["selection_evidence"] = json.loads(raw) if raw else None
            out.append(item)
        return out

    def replace_baseline(self, workflow_id: str, period: str, counts: dict) -> None:
        with self.transaction():
            self.conn.execute(
                """INSERT INTO baselines (
                       workflow_id, period, dials, connected, engaged, leads_uploaded, leads_concluded, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id, period) DO UPDATE SET
                     dials = excluded.dials,
                     connected = excluded.connected,
                     engaged = excluded.engaged,
                     leads_uploaded = excluded.leads_uploaded,
                     leads_concluded = excluded.leads_concluded,
                     updated_at = excluded.updated_at""",
                (
                    workflow_id,
                    period,
                    int(counts["dials"]),
                    int(counts["connected"]),
                    int(counts["engaged"]),
                    int(counts["leads_uploaded"]),
                    int(counts["leads_concluded"]),
                    _now(),
                ),
            )

    def save_snapshot(self, document: dict) -> int:
        with self.transaction():
            cur = self.conn.execute(
                """INSERT INTO supervisor_snapshots (sim_minute, sim_ts, created_at, document_json)
                   VALUES (?, ?, ?, ?)""",
                (self.sim_minute, self.sim_ts, _now(), _dumps(document)),
            )
            return int(cur.lastrowid)

    def latest_snapshot(self) -> dict | None:
        row = self.conn.execute(
            "SELECT document_json FROM supervisor_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["document_json"])

    def latest_config(self, workflow_id: str) -> dict | None:
        row = self.conn.execute(
            """SELECT version, actor, applied_at, config_json, source
               FROM config_snapshots WHERE workflow_id = ?
               ORDER BY version DESC LIMIT 1""",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "workflow_id": workflow_id,
            "version": int(row["version"]),
            "actor": row["actor"],
            "applied_at": row["applied_at"],
            "source": row["source"],
            "config": json.loads(row["config_json"]),
        }

    def pending_approval_count(self, workflow_id: str) -> int:
        row = self.conn.execute(
            """SELECT COUNT(*) AS n FROM approvals_seen
               WHERE workflow_id = ? AND status = 'pending'""",
            (workflow_id,),
        ).fetchone()
        return int(row["n"])

    def save_experiment(self, experiment: dict) -> bool:
        """Insert a design. Returns False when that id already exists."""
        now = _now()
        with self.transaction():
            before = self.conn.total_changes
            self.conn.execute(
                """INSERT OR IGNORE INTO experiments (
                       experiment_id, workflow_id, campaign_id, cohort_id, dimension, hypothesis,
                       primary_metric, guardrail_metric, guardrail_max_drop, control_value,
                       treatment_value, baseline_rate, minimum_detectable_effect, alpha, power,
                       allocation_control, allocation_treatment, required_control, required_treatment,
                       sample_unit, status, executable, exposure_verified, causal, base_version,
                       proposed_config_json, execution_limit, latest_decision, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    experiment["experiment_id"],
                    experiment["workflow_id"],
                    experiment.get("campaign_id"),
                    experiment["cohort_id"],
                    experiment["dimension"],
                    experiment["hypothesis"],
                    experiment["primary_metric"],
                    experiment["guardrail_metric"],
                    float(experiment["guardrail_max_drop"]),
                    experiment["control_value"],
                    experiment["treatment_value"],
                    float(experiment["baseline_rate"]),
                    float(experiment["minimum_detectable_effect"]),
                    float(experiment["alpha"]),
                    float(experiment["power"]),
                    float(experiment["allocation_control"]),
                    float(experiment["allocation_treatment"]),
                    int(experiment["required_control"]),
                    int(experiment["required_treatment"]),
                    experiment["sample_unit"],
                    experiment["status"],
                    1 if experiment["executable"] else 0,
                    1 if experiment["exposure_verified"] else 0,
                    1 if experiment["causal"] else 0,
                    int(experiment["base_version"]),
                    _dumps(experiment["proposed_config"]),
                    experiment["execution_limit"],
                    experiment.get("latest_decision"),
                    now,
                    now,
                ),
            )
            inserted = self.conn.total_changes != before
            if inserted:
                self.conn.execute(
                    """INSERT INTO experiment_transitions
                       (experiment_id, from_status, to_status, reason, created_at)
                       VALUES (?, NULL, ?, ?, ?)""",
                    (experiment["experiment_id"], experiment["status"], "design stored", now),
                )
        return inserted

    def get_experiment(self, experiment_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        return None if row is None else _experiment_row(row)

    def experiments_for(self, workflow_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self.conn.execute(
                f"""SELECT * FROM experiments WHERE workflow_id = ? AND status IN ({marks})
                    ORDER BY experiment_id""",
                (workflow_id, *statuses),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM experiments WHERE workflow_id = ? ORDER BY experiment_id",
                (workflow_id,),
            ).fetchall()
        return [_experiment_row(row) for row in rows]

    def transition_experiment(self, experiment_id: str, status: str, reason: str, decision: str | None = None) -> None:
        current = self.get_experiment(experiment_id)
        if current is None:
            raise KeyError(experiment_id)
        now = _now()
        with self.transaction():
            self.conn.execute(
                """UPDATE experiments
                   SET status = ?, latest_decision = COALESCE(?, latest_decision), updated_at = ?
                   WHERE experiment_id = ?""",
                (status, decision, now, experiment_id),
            )
            self.conn.execute(
                """INSERT INTO experiment_transitions
                   (experiment_id, from_status, to_status, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (experiment_id, current["status"], status, reason, now),
            )

    def save_candidates(self, workflow_id: str, candidates: list[dict], experiment_id: str | None = None) -> None:
        now = _now()
        with self.transaction():
            for candidate in candidates:
                self.conn.execute(
                    """INSERT INTO experiment_candidates (
                           experiment_id, workflow_id, source, dimension, current_value, proposed_value,
                           accepted, selected, reason, raw_json, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment_id,
                        workflow_id,
                        candidate["source"],
                        candidate["dimension"],
                        candidate.get("current_value"),
                        candidate.get("proposed_value"),
                        1 if candidate["accepted"] else 0,
                        1 if candidate.get("selected") else 0,
                        candidate["reason"],
                        _dumps(candidate.get("raw") if candidate.get("raw") is not None else candidate),
                        now,
                    ),
                )

    def save_experiment_decision(self, decision: dict) -> None:
        with self.transaction():
            self.conn.execute(
                """INSERT INTO experiment_decisions (
                       workflow_id, experiment_id, kind, eligible, decision, reason, evidence_json,
                       causal, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision["workflow_id"],
                    decision.get("experiment_id"),
                    decision["kind"],
                    None if decision.get("eligible") is None else int(bool(decision["eligible"])),
                    decision["decision"],
                    decision["reason"],
                    _dumps(decision.get("evidence") or {}),
                    1 if decision.get("causal") else 0,
                    _now(),
                ),
            )

    def save_assignments(self, experiment_id: str, rows: list[dict]) -> int:
        """Insert planned arms. An existing lead keeps the arm it already has."""
        now = _now()
        inserted = 0
        with self.transaction():
            for row in rows:
                before = self.conn.total_changes
                self.conn.execute(
                    """INSERT OR IGNORE INTO experiment_assignments
                       (experiment_id, lead_id, arm, reason, planned, created_at)
                       VALUES (?, ?, ?, ?, 1, ?)""",
                    (experiment_id, row["lead_id"], row["arm"], row["reason"], now),
                )
                if self.conn.total_changes != before:
                    inserted += 1
        return inserted

    def assignment_rows(self, experiment_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT lead_id, arm, reason, planned FROM experiment_assignments
               WHERE experiment_id = ? ORDER BY lead_id""",
            (experiment_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def conflicting_assignment(self, workflow_id: str, dimension: str, lead_id: str, experiment_id: str) -> str | None:
        row = self.conn.execute(
            """SELECT a.experiment_id FROM experiment_assignments a
               JOIN experiments e ON e.experiment_id = a.experiment_id
               WHERE a.lead_id = ? AND e.workflow_id = ? AND e.dimension = ?
                 AND e.experiment_id != ? AND e.status IN ('designed', 'running')
                 AND a.arm IN ('control', 'treatment')""",
            (lead_id, workflow_id, dimension, experiment_id),
        ).fetchone()
        return None if row is None else str(row["experiment_id"])

    def save_observation(self, experiment_id: str, arm: str, counts: dict) -> None:
        """Store absolute arm counts. Aggregate rows are not exposure proof."""
        with self.transaction():
            self.conn.execute(
                """INSERT INTO experiment_observations (
                       experiment_id, arm, dials, connected, engaged, leads_uploaded, leads_concluded,
                       exposure_verified, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(experiment_id, arm) DO UPDATE SET
                     dials = excluded.dials,
                     connected = excluded.connected,
                     engaged = excluded.engaged,
                     leads_uploaded = excluded.leads_uploaded,
                     leads_concluded = excluded.leads_concluded,
                     exposure_verified = 0,
                     updated_at = excluded.updated_at""",
                (
                    experiment_id,
                    arm,
                    int(counts["dials"]),
                    int(counts["connected"]),
                    int(counts["engaged"]),
                    counts.get("leads_uploaded"),
                    counts.get("leads_concluded"),
                    _now(),
                ),
            )

    def observation_rows(self, experiment_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM experiment_observations WHERE experiment_id = ? ORDER BY arm",
            (experiment_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_evaluation(self, experiment_id: str, result: dict) -> bool:
        """Append an evaluation. A second confirmatory look is not stored."""
        confirmatory = bool(result.get("confirmatory"))
        now = _now()
        with self.transaction():
            if confirmatory and self.confirmatory_evaluation(experiment_id) is not None:
                return False
            self.conn.execute(
                """INSERT INTO experiment_evaluations
                   (experiment_id, decision, reason, evidence_json, causal, confirmatory, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    experiment_id,
                    result["decision"],
                    result["reason"],
                    _dumps(result),
                    1 if result.get("causal") else 0,
                    1 if confirmatory else 0,
                    now,
                ),
            )
            if confirmatory:
                self.conn.execute(
                    """UPDATE experiments
                       SET latest_decision = ?, causal = ?, exposure_verified = ?, updated_at = ?
                       WHERE experiment_id = ?""",
                    (
                        result["decision"],
                        1 if result.get("causal") else 0,
                        1 if (result.get("evidence") or {}).get("exposure_verified") else 0,
                        now,
                        experiment_id,
                    ),
                )
            elif self.confirmatory_evaluation(experiment_id) is None:
                self.conn.execute(
                    "UPDATE experiments SET latest_decision = ?, updated_at = ? WHERE experiment_id = ?",
                    (result["decision"], now, experiment_id),
                )
        return True

    def confirmatory_evaluation(self, experiment_id: str) -> dict | None:
        row = self.conn.execute(
            """SELECT decision, reason, evidence_json, causal, created_at
               FROM experiment_evaluations
               WHERE experiment_id = ? AND confirmatory = 1
               ORDER BY id LIMIT 1""",
            (experiment_id,),
        ).fetchone()
        if row is None:
            return None
        stored = json.loads(row["evidence_json"])
        stored["causal"] = bool(row["causal"])
        return stored

    def evaluation_rows(self, experiment_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT decision, reason, evidence_json, causal, confirmatory, created_at
               FROM experiment_evaluations WHERE experiment_id = ? ORDER BY id""",
            (experiment_id,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["causal"] = bool(item["causal"])
            item["confirmatory"] = bool(item["confirmatory"])
            out.append(item)
        return out

    def summary(self) -> dict:
        counts = {f"{kind}|{event_type}": n for (kind, event_type), n in self.ingest_counts().items()}
        return {
            "scenario": self.scenario,
            "sim_minute": self.sim_minute,
            "sim_ts": self.sim_ts,
            "last_chunk": self.last_chunk,
            "last_event_id": self.last_event_id,
            "pending_advance_from_minute": self.pending_advance_from_minute,
            "counts": counts,
            "kpis": self.kpis(),
            "call_totals": self.call_totals(),
            "leads": self.lead_count(),
        }


def _experiment_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["proposed_config"] = json.loads(item.pop("proposed_config_json"))
    item["executable"] = bool(item["executable"])
    item["exposure_verified"] = bool(item["exposure_verified"])
    item["causal"] = bool(item["causal"])
    return item


def _kpi_row(row: sqlite3.Row) -> dict:
    dials = int(row["dials"])
    connected = int(row["connected"])
    engaged = int(row["engaged"])
    uploaded = int(row["leads_uploaded"])
    concluded = int(row["leads_concluded"])
    return {
        "dials": dials,
        "connected": connected,
        "engaged": engaged,
        "leads_uploaded": uploaded,
        "leads_concluded": concluded,
        "pickup_rate": None if dials == 0 else connected / dials,
        "engagement_rate": None if connected == 0 else engaged / connected,
        "business_outcome": None if uploaded == 0 else concluded / uploaded,
    }


def _hour_and_date(ts: str) -> tuple[int, str]:
    # 2027-01-04T09:15:00+05:30
    date, rest = ts.split("T", 1)
    hour = int(rest.split(":", 1)[0])
    return hour, date
