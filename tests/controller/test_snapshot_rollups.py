"""Grouped snapshot totals must match the previous scans of the same rows."""

import time

from controller.analytics import (
    call_groups,
    campaign_counts,
    counts_for_features,
    feature_levels,
    period_counts,
    upload_day_counts,
)
from controller.cohorts import CohortSet
from controller.settings import SIM_DAY_ZERO, RunSettings
from controller.store import Store
from controller.supervisor import publish_snapshot


def _lead(event_id, workflow, campaign, person, ts, age, lang):
    return {
        "type": "lead",
        "ts": ts,
        "workflow_id": workflow,
        "campaign_id": campaign,
        "lead_id": f"{workflow}:{person}",
        "person_id": f"{workflow}:p{person}",
        "features": {"lang": lang, "city_tier": "1", "age_band": age, "value_band": "mid"},
        "event_id": event_id,
    }


def _call(event_id, workflow, campaign, person, ts, connected, engaged, caller):
    return {
        "type": "call",
        "ts": ts,
        "workflow_id": workflow,
        "campaign_id": campaign,
        "lead_id": f"{workflow}:{person}",
        "person_id": f"{workflow}:p{person}",
        "attempt_no": 1,
        "campaign_day_index": 0,
        "config_version": 1,
        "cohort_id": "default",
        "variant": {
            "welcome_message": "W1",
            "voice": "V1",
            "caller_id": caller,
            "prompt_version": "P1",
        },
        "connected": connected,
        "engaged": engaged,
        "disposition": "concluded_interested" if engaged else ("short_hangup" if connected else "no_answer"),
        "qa": None,
        "event_id": event_id,
    }


def _load(store: Store) -> None:
    events = []
    event_id = 1
    for workflow, campaign, day, kind in (
        ("north", "north:c1", "2026-12-20", "history"),
        ("south", "south:c1", "2026-12-20", "history"),
    ):
        events.append({
            "type": "campaign_created",
            "ts": f"{day}T08:00:00+05:30",
            "workflow_id": workflow,
            "campaign_id": campaign,
            "tenant_id": workflow,
            "leads": 4,
            "config_version_at_creation": 1,
            "event_id": event_id,
        })
        event_id += 1
        for index, age in enumerate(("18-30", "60+")):
            for person in range(2):
                who = f"{workflow[0]}{index}{person}"
                events.append(_lead(event_id, workflow, campaign, who, f"{day}T08:00:00+05:30", age, "hi" if person == 0 else "en"))
                event_id += 1
                events.append(_call(
                    event_id, workflow, campaign, who, f"{day}T09:00:00+05:30",
                    connected=True, engaged=person == 0, caller="D1" if index == 0 else "D2",
                ))
                event_id += 1
        store.apply_chunk({"chunk": 0 if workflow == "north" else 1, "until_ts": f"{day}T09:00:00+05:30"}, events, kind)
        events = []
    live = [{
        "type": "campaign_created",
        "ts": f"{SIM_DAY_ZERO}T08:00:00+05:30",
        "workflow_id": "north",
        "campaign_id": "north:c2",
        "tenant_id": "north",
        "leads": 2,
        "config_version_at_creation": 1,
        "event_id": event_id,
    }]
    event_id += 1
    for person in range(2):
        who = f"live{person}"
        live.append(_lead(event_id, "north", "north:c2", who, f"{SIM_DAY_ZERO}T08:00:00+05:30", "18-30", "hi"))
        event_id += 1
        live.append(_call(
            event_id, "north", "north:c2", who, f"{SIM_DAY_ZERO}T11:00:00+05:30",
            connected=person == 0, engaged=False, caller="D1",
        ))
        event_id += 1
    store.apply_chunk({"chunk": 2, "until_ts": f"{SIM_DAY_ZERO}T11:00:00+05:30"}, live, "events")
    again = store.apply_chunk({"chunk": 2, "until_ts": f"{SIM_DAY_ZERO}T11:00:00+05:30"}, live, "events")
    assert again == []


def _oracle_period(store: Store, workflow_id: str, period: str) -> tuple[int, int, int, int, int]:
    kind_sql = ""
    lead_sql = ""
    params: tuple = (workflow_id,)
    if period == "history":
        kind_sql = " AND kind = 'history'"
        lead_sql = " AND substr(ts, 1, 10) < ?"
        params = (workflow_id, SIM_DAY_ZERO)
    elif period == "live":
        kind_sql = " AND kind = 'events'"
        lead_sql = " AND substr(ts, 1, 10) >= ?"
        params = (workflow_id, SIM_DAY_ZERO)
    calls = store.conn.execute(
        f"""SELECT COALESCE(SUM(dials), 0), COALESCE(SUM(connected), 0), COALESCE(SUM(engaged), 0)
            FROM call_agg WHERE workflow_id = ?{kind_sql}""",
        (workflow_id,),
    ).fetchone()
    leads = store.conn.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(concluded), 0)
            FROM leads WHERE workflow_id = ?{lead_sql}""",
        params,
    ).fetchone()
    return tuple(int(value) for value in (*calls, *leads))


def _oracle_groups(store: Store, workflow_id: str, column: str, kind: str | None) -> list[tuple]:
    sql = f"""SELECT {column}, COALESCE(SUM(dials), 0), COALESCE(SUM(connected), 0), COALESCE(SUM(engaged), 0)
              FROM call_agg WHERE workflow_id = ?"""
    params: list = [workflow_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += f" GROUP BY {column} ORDER BY {column}"
    return [tuple(row) for row in store.conn.execute(sql, params)]


def _oracle_campaigns(store: Store, workflow_id: str) -> list[tuple]:
    rows = store.conn.execute(
        """SELECT c.campaign_id,
                  COALESCE(SUM(a.dials), 0),
                  COALESCE(SUM(a.connected), 0),
                  COALESCE(SUM(a.engaged), 0),
                  (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.campaign_id),
                  (SELECT COALESCE(SUM(concluded), 0) FROM leads l WHERE l.campaign_id = c.campaign_id)
           FROM campaigns c
           LEFT JOIN call_agg a ON a.workflow_id = c.workflow_id AND a.campaign_id = c.campaign_id
           WHERE c.workflow_id = ?
           GROUP BY c.campaign_id
           ORDER BY c.ts, c.campaign_id""",
        (workflow_id,),
    ).fetchall()
    return [tuple(row) for row in rows]


def _assert_matches_scans(store: Store) -> None:
    for workflow_id in ("north", "south"):
        for period in ("history", "live", "all"):
            counted = period_counts(store, workflow_id, period)
            assert (
                counted["dials"],
                counted["connected"],
                counted["engaged"],
                counted["leads_uploaded"],
                counted["leads_concluded"],
            ) == _oracle_period(store, workflow_id, period)
        for column in ("hour", "caller_id", "lang", "config_version", "call_date"):
            for kind in (None, "history", "events"):
                got = [(row["key"], row["dials"], row["connected"], row["engaged"]) for row in call_groups(store, workflow_id, column, kind=kind)]
                assert got == _oracle_groups(store, workflow_id, column, kind)
        got_campaigns = [
            (row["campaign_id"], row["dials"], row["connected"], row["engaged"], row["leads_uploaded"], row["leads_concluded"])
            for row in campaign_counts(store, workflow_id)
        ]
        assert got_campaigns == _oracle_campaigns(store, workflow_id)
        oracle_days = store.conn.execute(
            """SELECT substr(ts, 1, 10), COUNT(*), COALESCE(SUM(concluded), 0)
               FROM leads WHERE workflow_id = ? GROUP BY 1 ORDER BY 1""",
            (workflow_id,),
        ).fetchall()
        got_days = upload_day_counts(store, workflow_id)
        assert [row["day"] for row in got_days] == [row[0] for row in oracle_days]
        assert [row["leads_uploaded"] for row in got_days] == [int(row[1]) for row in oracle_days]
        assert [row["leads_concluded"] for row in got_days] == [int(row[2]) for row in oracle_days]
        for period in ("history", "live", "all"):
            levels, total = feature_levels(store, workflow_id, period)
            assert total == _oracle_period(store, workflow_id, period)[3]
            kind = {"history": "history", "live": "events", "all": None}[period]
            lead_sql = "SELECT lang, COUNT(*) FROM leads WHERE workflow_id = ?"
            params: list = [workflow_id]
            if period == "history":
                lead_sql += " AND substr(ts, 1, 10) < ?"
                params.append(SIM_DAY_ZERO)
            elif period == "live":
                lead_sql += " AND substr(ts, 1, 10) >= ?"
                params.append(SIM_DAY_ZERO)
            lead_sql += " GROUP BY 1"
            expected = []
            calls = {str(row[0]): row for row in _oracle_groups(store, workflow_id, "lang", kind)}
            for value, leads in store.conn.execute(lead_sql, params):
                call = calls.get(str(value))
                expected.append((
                    "lang",
                    str(value),
                    int(leads),
                    0 if call is None else int(call[1]),
                    0 if call is None else int(call[2]),
                    0 if call is None else int(call[3]),
                ))
            got_levels = [
                (level.feature, level.value, level.leads, level.dials, level.connected, level.engaged)
                for level in levels if level.feature == "lang"
            ]
            assert sorted(got_levels) == sorted(expected)
        counted = counts_for_features(store, workflow_id, CohortSet(()), kind="history")
        dials = sum(row["dials"] for row in counted.values())
        oracle = store.conn.execute(
            "SELECT COALESCE(SUM(dials), 0) FROM call_agg WHERE workflow_id = ? AND kind = 'history'",
            (workflow_id,),
        ).fetchone()[0]
        assert dials == int(oracle)
    south = period_counts(store, "south", "all")
    north = period_counts(store, "north", "all")
    assert south["dials"] == 4
    assert north["dials"] == 6
    assert south["leads_uploaded"] == 4
    assert north["leads_uploaded"] == 6
    assert period_counts(store, "south", "live")["dials"] == 0
    assert period_counts(store, "north", "live")["dials"] == 2


def test_grouped_totals_match_table_scans_and_survive_replay_and_rebuild(tmp_path):
    store = Store.open(tmp_path, "rollups")
    _load(store)
    _assert_matches_scans(store)
    for workflow_id in ("north", "south"):
        store.save_workflow({
            "workflow_id": workflow_id,
            "tenant_id": workflow_id,
            "current_version": 1,
            "objective": {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}},
        })
    started = time.perf_counter()
    snapshot = publish_snapshot(store, RunSettings())
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert snapshot["workflows"]
    assert store.proposal_count() == 0
    store.conn.execute("DELETE FROM call_group_totals")
    store.conn.execute("DELETE FROM lead_group_totals")
    store._rebuild_group_totals()
    _assert_matches_scans(store)
    store.close()

    reopened = Store.open(tmp_path, "rollups")
    _assert_matches_scans(reopened)
    reopened.close()
