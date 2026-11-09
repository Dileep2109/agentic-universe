"""Read-only KPI queries.

Pickup is connected/dials, engagement is engaged/connected, and business
outcome is leads concluded/leads uploaded. A slice with no lead denominator
(an hour, a config version) leaves business outcome unset instead of borrowing
another rate.
"""

from __future__ import annotations

from .cohorts import FEATURE_VALUES, CohortSet, LevelCounts
from .store import Store, unpack_key

_NUMERIC_KEYS = {"hour", "config_version", "campaign_day_index"}

_CALL_COLUMNS = (
    "kind",
    "campaign_id",
    "config_version",
    "call_date",
    "hour",
    "lang",
    "city_tier",
    "age_band",
    "value_band",
    "welcome_message",
    "voice",
    "caller_id",
    "prompt_version",
)


def rate_block(
    *,
    dials: int,
    connected: int,
    engaged: int,
    leads_uploaded: int | None = None,
    leads_concluded: int | None = None,
) -> dict:
    block = {
        "dials": dials,
        "connected": connected,
        "engaged": engaged,
        "leads_uploaded": leads_uploaded,
        "leads_concluded": leads_concluded,
        "pickup_rate": None if dials == 0 else connected / dials,
        "engagement_rate": None if connected == 0 else engaged / connected,
        "business_outcome": None,
    }
    if leads_uploaded:
        block["business_outcome"] = leads_concluded / leads_uploaded if leads_concluded is not None else None
    elif leads_uploaded == 0:
        block["business_outcome"] = None
    return block


def _typed_key(column: str, raw: str):
    if column in _NUMERIC_KEYS:
        return int(raw)
    return raw


def call_groups(store: Store, workflow_id: str, column: str, *, kind: str | None = None) -> list[dict]:
    if column not in _CALL_COLUMNS:
        raise ValueError(f"cannot group calls by {column}")
    sql = """
        SELECT group_key AS key,
               COALESCE(SUM(dials), 0) AS dials,
               COALESCE(SUM(connected), 0) AS connected,
               COALESCE(SUM(engaged), 0) AS engaged
        FROM call_group_totals
        WHERE workflow_id = ? AND grain = ?
    """
    params: list = [workflow_id, column]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " GROUP BY group_key"
    rows = store.conn.execute(sql, params).fetchall()
    grouped = [
        {
            "key": _typed_key(column, row["key"]),
            **rate_block(dials=int(row["dials"]), connected=int(row["connected"]), engaged=int(row["engaged"])),
        }
        for row in rows
    ]
    grouped.sort(key=lambda item: item["key"])
    return grouped


def _connection_row(key, dials: int, connected: int, engaged: int, no_answer: int, voicemail: int) -> dict:
    """Connection health from a call event.

    ``connected`` is the pickup outcome. ``no_answer`` and ``voicemail`` are the
    documented dispositions for a dial that never connected. ``GET /health`` is
    only process liveness and is not a caller-ID measure.
    """
    unanswered = no_answer + voicemail
    row = rate_block(dials=dials, connected=connected, engaged=engaged)
    row.update({
        "key": key,
        "no_answer": no_answer,
        "voicemail": voicemail,
        "unanswered": unanswered,
        "unanswered_rate": None if dials == 0 else unanswered / dials,
    })
    return row


def caller_connection_health(
    store: Store,
    workflow_id: str,
    *,
    kind: str = "events",
    config_version: int | None = None,
) -> list[dict]:
    """Per-caller connection counts for one period, optionally one config version."""
    sql = """
        SELECT caller_id AS key,
               COALESCE(SUM(dials), 0) AS dials,
               COALESCE(SUM(connected), 0) AS connected,
               COALESCE(SUM(engaged), 0) AS engaged,
               COALESCE(SUM(no_answer), 0) AS no_answer,
               COALESCE(SUM(voicemail), 0) AS voicemail
        FROM call_agg
        WHERE workflow_id = ? AND kind = ?
    """
    params: list = [workflow_id, kind]
    if config_version is not None:
        sql += " AND config_version = ?"
        params.append(int(config_version))
    sql += " GROUP BY caller_id ORDER BY caller_id"
    rows = store.conn.execute(sql, params).fetchall()
    return [
        _connection_row(
            row["key"],
            int(row["dials"]),
            int(row["connected"]),
            int(row["engaged"]),
            int(row["no_answer"]),
            int(row["voicemail"]),
        )
        for row in rows
    ]


def recent_caller_connection(
    store: Store,
    workflow_id: str,
    config_version: int,
    caller_ids: tuple[str, ...],
    min_dials: int,
    *,
    kind: str = "events",
) -> dict[str, dict] | None:
    """Newest hours of one version until each requested caller reaches ``min_dials``.

    Older hours are left out once the floor is met, so a long healthy history
    does not dilute a recent connection drop. Returns None when the version
    never reaches the floor for every requested caller.
    """
    wanted = tuple(dict.fromkeys(caller_ids))
    if len(wanted) < 2 or min_dials <= 0:
        return None
    placeholders = ", ".join("?" for _ in wanted)
    rows = store.conn.execute(
        f"""SELECT call_date, hour, caller_id,
                   COALESCE(SUM(dials), 0) AS dials,
                   COALESCE(SUM(connected), 0) AS connected,
                   COALESCE(SUM(engaged), 0) AS engaged,
                   COALESCE(SUM(no_answer), 0) AS no_answer,
                   COALESCE(SUM(voicemail), 0) AS voicemail
            FROM call_agg
            WHERE workflow_id = ? AND kind = ? AND config_version = ?
              AND caller_id IN ({placeholders})
            GROUP BY call_date, hour, caller_id
            ORDER BY call_date DESC, hour DESC""",
        (workflow_id, kind, int(config_version), *wanted),
    ).fetchall()
    buckets: list[list] = []
    index: dict[tuple, int] = {}
    for row in rows:
        key = (row["call_date"], int(row["hour"]))
        slot = index.get(key)
        if slot is None:
            index[key] = len(buckets)
            buckets.append([row])
        else:
            buckets[slot].append(row)
    totals = {caller: {"dials": 0, "connected": 0, "engaged": 0, "no_answer": 0, "voicemail": 0} for caller in wanted}
    fields = ("dials", "connected", "engaged", "no_answer", "voicemail")
    for bucket in buckets:
        for row in bucket:
            cell = totals[str(row["caller_id"])]
            for field in fields:
                cell[field] += int(row[field] or 0)
        if all(totals[caller]["dials"] >= min_dials for caller in wanted):
            return {
                caller: _connection_row(caller, *[cell[field] for field in fields])
                for caller, cell in totals.items()
            }
    return None


def _call_period(store: Store, workflow_id: str, period: str) -> tuple[int, int, int]:
    sql = """SELECT COALESCE(SUM(dials), 0) AS dials,
                    COALESCE(SUM(connected), 0) AS connected,
                    COALESCE(SUM(engaged), 0) AS engaged
             FROM call_group_totals
             WHERE workflow_id = ? AND grain = '_all'"""
    params: list = [workflow_id]
    if period == "history":
        sql += " AND kind = 'history'"
    elif period == "live":
        sql += " AND kind = 'events'"
    elif period != "all":
        raise ValueError("period must be history, live, or all")
    row = store.conn.execute(sql, params).fetchone()
    return int(row["dials"]), int(row["connected"]), int(row["engaged"])


def _lead_period_totals(store: Store, workflow_id: str, period: str) -> tuple[int, int]:
    if period == "all":
        row = store.conn.execute(
            """SELECT COALESCE(SUM(uploaded), 0) AS uploaded,
                      COALESCE(SUM(concluded), 0) AS concluded
               FROM lead_group_totals
               WHERE workflow_id = ? AND grain = 'period'""",
            (workflow_id,),
        ).fetchone()
    elif period in ("history", "live"):
        row = store.conn.execute(
            """SELECT uploaded, concluded FROM lead_group_totals
               WHERE workflow_id = ? AND grain = 'period' AND group_key = ?""",
            (workflow_id, period),
        ).fetchone()
        if row is None:
            return 0, 0
    else:
        raise ValueError("period must be history, live, or all")
    return int(row["uploaded"]), int(row["concluded"])


def period_counts(store: Store, workflow_id: str, period: str) -> dict:
    """Call rates for a period, and business outcome for leads uploaded in it.

    ``history`` is before documented D0. ``live`` is D0 onward. ``all`` is both.
    Totals come from grouped rows written in the same transaction as the events.
    """
    dials, connected, engaged = _call_period(store, workflow_id, period)
    uploaded, concluded = _lead_period_totals(store, workflow_id, period)
    return rate_block(
        dials=dials,
        connected=connected,
        engaged=engaged,
        leads_uploaded=uploaded,
        leads_concluded=concluded,
    )


def campaign_counts(store: Store, workflow_id: str) -> list[dict]:
    calls = {
        row["key"]: row
        for row in store.conn.execute(
            """SELECT group_key AS key,
                      COALESCE(SUM(dials), 0) AS dials,
                      COALESCE(SUM(connected), 0) AS connected,
                      COALESCE(SUM(engaged), 0) AS engaged
               FROM call_group_totals
               WHERE workflow_id = ? AND grain = 'campaign_id'
               GROUP BY group_key""",
            (workflow_id,),
        )
    }
    leads = {
        row["key"]: row
        for row in store.conn.execute(
            """SELECT group_key AS key, uploaded, concluded
               FROM lead_group_totals
               WHERE workflow_id = ? AND grain = 'campaign'""",
            (workflow_id,),
        )
    }
    campaigns = store.conn.execute(
        """SELECT campaign_id, ts, config_version_at_creation
           FROM campaigns WHERE workflow_id = ?
           ORDER BY ts, campaign_id""",
        (workflow_id,),
    ).fetchall()
    out = []
    for row in campaigns:
        call = calls.get(row["campaign_id"])
        lead = leads.get(row["campaign_id"])
        block = rate_block(
            dials=0 if call is None else int(call["dials"]),
            connected=0 if call is None else int(call["connected"]),
            engaged=0 if call is None else int(call["engaged"]),
            leads_uploaded=0 if lead is None else int(lead["uploaded"]),
            leads_concluded=0 if lead is None else int(lead["concluded"]),
        )
        block["campaign_id"] = row["campaign_id"]
        block["ts"] = row["ts"]
        block["config_version_at_creation"] = row["config_version_at_creation"]
        out.append(block)
    return out


def upload_day_counts(store: Store, workflow_id: str) -> list[dict]:
    """Business outcome for leads uploaded on each calendar day, plus that day's calls."""
    lead_rows = store.conn.execute(
        """SELECT group_key AS day, uploaded, concluded
           FROM lead_group_totals
           WHERE workflow_id = ? AND grain = 'day'
           ORDER BY group_key""",
        (workflow_id,),
    ).fetchall()
    call_rows = {
        row["key"]: row
        for row in call_groups(store, workflow_id, "call_date")
    }
    out = []
    for row in lead_rows:
        calls = call_rows.get(row["day"])
        block = rate_block(
            dials=0 if calls is None else calls["dials"],
            connected=0 if calls is None else calls["connected"],
            engaged=0 if calls is None else calls["engaged"],
            leads_uploaded=int(row["uploaded"]),
            leads_concluded=int(row["concluded"]),
        )
        block["day"] = row["day"]
        out.append(block)
    return out


def feature_levels(store: Store, workflow_id: str, period: str) -> tuple[list[LevelCounts], int]:
    kind = {"history": "history", "live": "events", "all": None}[period]
    total = period_counts(store, workflow_id, period)["leads_uploaded"] or 0
    levels: list[LevelCounts] = []
    lead_rows = store.conn.execute(
        """SELECT group_key, uploaded FROM lead_group_totals
           WHERE workflow_id = ? AND grain = 'feature'""",
        (workflow_id,),
    ).fetchall()
    for feature in FEATURE_VALUES:
        call_rows = {
            str(row["key"]): row
            for row in call_groups(store, workflow_id, feature, kind=kind)
        }
        leads_by_value: dict[str, int] = {}
        for row in lead_rows:
            lead_period, lead_feature, value = unpack_key(row["group_key"], 3)
            if lead_feature != feature:
                continue
            if period != "all" and lead_period != period:
                continue
            leads_by_value[value] = leads_by_value.get(value, 0) + int(row["uploaded"])
        for value, leads in sorted(leads_by_value.items()):
            calls = call_rows.get(value, {})
            levels.append(LevelCounts(
                feature=feature,
                value=value,
                leads=leads,
                dials=int(calls.get("dials", 0)),
                connected=int(calls.get("connected", 0)),
                engaged=int(calls.get("engaged", 0)),
            ))
    return levels, int(total)


def counts_for_features(store: Store, workflow_id: str, cohorts: CohortSet, *, kind: str | None) -> dict[str, dict]:
    """Sum calls into the shared cohort ids. The same CohortSet the snapshot stores."""
    sql = """
        SELECT group_key,
               SUM(dials) AS dials, SUM(connected) AS connected, SUM(engaged) AS engaged
        FROM call_group_totals
        WHERE workflow_id = ? AND grain = 'features'
    """
    params: list = [workflow_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " GROUP BY group_key"
    totals: dict[str, list[int]] = {}
    for row in store.conn.execute(sql, params):
        lang, city_tier, age_band, value_band = unpack_key(row["group_key"], 4)
        cohort_id = cohorts.assign({
            "lang": lang,
            "city_tier": city_tier,
            "age_band": age_band,
            "value_band": value_band,
        })
        bucket = totals.setdefault(cohort_id, [0, 0, 0])
        bucket[0] += int(row["dials"])
        bucket[1] += int(row["connected"])
        bucket[2] += int(row["engaged"])
    return {
        cohort_id: rate_block(dials=dials, connected=connected, engaged=engaged)
        for cohort_id, (dials, connected, engaged) in sorted(totals.items())
    }
