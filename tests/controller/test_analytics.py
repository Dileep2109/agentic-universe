"""Analytics, guardrails, and the read-only supervisor snapshot."""

import sqlite3

from controller.analytics import period_counts, rate_block
from controller.monitor import TESTING_FAMILIES, assess_change, assess_guardrail, assess_slices
from controller.settings import SIM_DAY_ZERO, CohortPolicy, DecisionThresholds, RunSettings
from controller.stats import compare_rates
from controller.store import Store, _CALL_AGG_DDL
from controller.supervisor import _cohort_findings, _period_findings, publish_snapshot


THRESHOLDS = DecisionThresholds(
    alpha=0.05,
    pickup_min_dials=200,
    engagement_min_connected=200,
    business_min_leads=200,
    min_abs_change=0.02,
    confidence=0.95,
)


def _counts(successes, n, metric):
    if metric == "pickup_rate":
        return {"dials": n, "connected": successes, "engaged": 0}
    if metric == "engagement_rate":
        return {"dials": n, "connected": n, "engaged": successes}
    return {"leads_uploaded": n, "leads_concluded": successes}


def test_three_rates_stay_independent_and_business_needs_leads():
    block = rate_block(dials=100, connected=40, engaged=10, leads_uploaded=50, leads_concluded=8)
    assert block["pickup_rate"] == 0.40
    assert block["engagement_rate"] == 0.25
    assert block["business_outcome"] == 0.16
    hour = rate_block(dials=100, connected=40, engaged=10)
    assert hour["business_outcome"] is None
    assert hour["pickup_rate"] != hour["engagement_rate"]


def test_degradation_is_confirmed_only_past_noise_and_the_sample_floor():
    baseline = _counts(400, 1000, "pickup_rate")
    dropped = _counts(250, 1000, "pickup_rate")
    confirmed = assess_change(baseline, dropped, "pickup_rate", THRESHOLDS, subject="live")
    assert confirmed["status"] == "confirmed"
    assert confirmed["direction"] == "down"

    noisy = _counts(2, 20, "pickup_rate")
    base_small = _counts(10, 20, "pickup_rate")
    small = assess_change(base_small, noisy, "pickup_rate", THRESHOLDS, subject="live")
    assert small["status"] == "insufficient"
    assert small["status"] != "confirmed"

    mild = _counts(390, 1000, "pickup_rate")
    variation = assess_change(baseline, mild, "pickup_rate", THRESHOLDS, subject="live")
    assert variation["status"] in ("within_variation", "observation")
    assert variation["status"] != "confirmed"


def test_guardrail_is_independent_of_the_primary_lift():
    objective = {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}
    baseline = {"dials": 2000, "connected": 800, "engaged": 400}
    # Engagement rises, pickup falls by 6pp, past the 2pp guardrail.
    current = {"dials": 2000, "connected": 680, "engaged": 420}
    primary = assess_change(baseline, current, "engagement_rate", THRESHOLDS)
    guard = assess_guardrail(objective, baseline, current, THRESHOLDS)
    assert primary["metric"] == "engagement_rate"
    assert primary["direction"] == "up"
    assert guard is not None
    assert guard["role"] == "guardrail"
    assert guard["metric"] == "pickup_rate"
    assert guard["status"] == "confirmed"
    assert primary["status"] != guard["status"] or primary["metric"] != guard["metric"]

    within = {"dials": 2000, "connected": 790, "engaged": 400}
    allowed = assess_guardrail(objective, baseline, within, THRESHOLDS)
    assert allowed is not None and allowed["status"] != "confirmed"


def test_snapshot_uses_one_cohort_definition_and_submits_nothing(tmp_path):
    store = Store.open(tmp_path, "analytics")
    _load_two_ages(store)
    store.save_workflow({
        "workflow_id": "nw_renewal",
        "tenant_id": "northwind_life",
        "current_version": 1,
        "objective": {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}},
    })
    before = store.call_totals()
    snapshot = publish_snapshot(store, RunSettings(
        thresholds=DecisionThresholds(
            alpha=0.05, pickup_min_dials=500, engagement_min_connected=200, min_abs_change=0.02
        ),
        cohorts=CohortPolicy(min_lead_share=0.2, min_dials=500, max_cohorts=6),
    ))
    assert store.proposal_count() == 0
    assert store.call_totals() == before
    view = snapshot["workflows"][0]
    definitions = view["shared_cohorts"]["definitions"]
    assert {row["cohort_id"] for row in definitions} == {"age_band:18-30", "age_band:60+"}
    assert set(view["shared_cohorts"]["performance"]) == {"age_band:18-30", "age_band:60+"}
    assert view["baselines"]["history"]["pickup_rate"] != view["baselines"]["history"]["engagement_rate"]
    assert view["baselines"]["history"]["business_outcome"] is not None
    assert all(item["business_outcome"] is None for item in view["config_versions"])
    assert snapshot["incidents"] == []
    evidence = view["shared_cohorts"]["selection_evidence"]
    assert evidence["compared_levels"] == ["18-30", "60+"]
    assert evidence["family"] in TESTING_FAMILIES
    for row in definitions:
        assert "independently demonstrated" in row["reason"]
        assert row["selection_evidence"]["compared_levels"] == evidence["compared_levels"]
    assert view["shared_cohorts"]["missing_features"]["dials"] == 0
    assert "excluded" in view["shared_cohorts"]["missing_features"]["policy"]
    for finding in view["versus_history"]:
        assert finding["causal"] is False
        assert "observational" in finding["observational_warning"]
        assert "not a causal" in finding["observational_warning"]
    for finding in view["confirmed"]:
        assert finding["status"] == "confirmed"
        assert "not an incident" in finding["note"]
        assert finding["evidence"]["p_value"] is not None
    for finding in view["observations"]:
        assert finding["status"] in ("observation", "insufficient")
    again = store.latest_snapshot()
    assert again["workflows"][0]["shared_cohorts"]["definitions"] == definitions
    store.close()


def test_missing_calls_are_reported_and_left_out_of_the_complement():
    counts = {
        "age_band:18-30": {"dials": 1000, "connected": 200, "engaged": 50},
        "default": {"dials": 1000, "connected": 500, "engaged": 100},
        "missing": {"dials": 5000, "connected": 100, "engaged": 10},
    }
    settings = RunSettings(thresholds=DecisionThresholds(
        alpha=0.05, pickup_min_dials=200, engagement_min_connected=40, min_abs_change=0.02,
        guardrail_confidence=0.90,
    ))
    findings, missing = _cohort_findings(
        counts,
        "pickup_rate",
        {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}},
        settings,
    )
    assert missing["dials"] == 5000
    assert "not mixed" in missing["policy"]
    primary = {item["slice"]: item for item in findings if item["role"] == "primary"}
    # Complement is default only. The 5000 missing dials are not in it.
    assert primary["age_band:18-30"]["evidence"]["right"]["n"] == 1000
    assert primary["age_band:18-30"]["missing_excluded"]["dials"] == 5000
    assert primary["age_band:18-30"]["family"] == "cohort_contrast_primary"
    guards = [item for item in findings if item["role"] == "guardrail"]
    assert guards
    assert guards[0]["family"] == "cohort_contrast_guardrail"
    assert guards[0]["family"] != primary["age_band:18-30"]["family"]
    assert guards[0]["evidence"]["adjusted_alpha"] == settings.thresholds.guardrail_alpha() / 2

    alone, alone_missing = _cohort_findings(
        {
            "age_band:60+": {"dials": 1000, "connected": 400, "engaged": 100},
            "missing": {"dials": 4000, "connected": 400, "engaged": 40},
        },
        "pickup_rate",
        {"primary": "pickup_rate"},
        settings,
    )
    assert alone_missing["dials"] == 4000
    assert alone[0]["status"] == "insufficient"
    assert alone[0]["evidence"]["right"]["n"] == 0


def test_live_versus_history_is_observational_and_bonferroni_separated_from_guardrails():
    thresholds = DecisionThresholds(
        alpha=0.05, pickup_min_dials=200, engagement_min_connected=200,
        business_min_leads=200, min_abs_change=0.02, guardrail_confidence=0.90,
    )
    history = {"dials": 3000, "connected": 1200, "engaged": 600, "leads_uploaded": 3000, "leads_concluded": 600}
    live = {"dials": 3000, "connected": 1290, "engaged": 645, "leads_uploaded": 3000, "leads_concluded": 600}
    raw = compare_rates(1290, 3000, 1200, 3000, alpha=0.05, minimum_n=200, min_abs_change=0.02)
    adjusted = compare_rates(1290, 3000, 1200, 3000, alpha=0.05 / 3, minimum_n=200, min_abs_change=0.02)
    assert raw.supported_side() is not None
    assert adjusted.supported_side() is None
    findings = _period_findings(
        history,
        live,
        "pickup_rate",
        {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}},
        RunSettings(thresholds=thresholds),
    )
    pickup = next(item for item in findings if item["metric"] == "pickup_rate" and item["role"] == "primary")
    assert pickup["status"] != "confirmed"
    assert pickup["family"] == "live_vs_history_kpis"
    assert pickup["family_size"] == 3
    assert pickup["evidence"]["adjusted_alpha"] == thresholds.alpha / 3
    assert pickup["causal"] is False
    assert "observational" in pickup["observational_warning"]
    assert "not a causal" in pickup["observational_warning"]
    guard = next(item for item in findings if item["role"] == "guardrail")
    assert guard["family"] == "live_vs_history_guardrail"
    assert guard["evidence"]["alpha"] == thresholds.guardrail_alpha()
    assert guard["family"] != pickup["family"]

    # A guardrail excess whose p-value sits between primary alpha and guardrail alpha.
    baseline = {"dials": 2000, "connected": 800, "engaged": 400}
    current = {"dials": 2000, "connected": 715, "engaged": 400}
    breach = assess_guardrail(
        {"guardrail": {"pickup_rate_max_drop": 0.02}}, baseline, current, thresholds
    )
    assert breach is not None
    assert 0.05 < breach["p_value"] < thresholds.guardrail_alpha()
    assert breach["status"] == "confirmed"
    assert breach["causal"] is False
    assert "not an incident" in breach["note"]


def test_slice_family_bonferroni_covers_every_testable_slice():
    slices = []
    for hour, connected in ((9, 300), (10, 300), (11, 450)):
        slices.append({
            "hour": hour,
            "dials": 1000,
            "connected": connected,
            "engaged": 100,
            "_total_dials": 3000,
            "_total_connected": 1050,
            "_total_engaged": 300,
        })
    findings = assess_slices(
        slices, "pickup_rate", THRESHOLDS, subject_key="hour", family="hour_contrast_pickup"
    )
    assert len(findings) == 3
    assert all(item["family"] == "hour_contrast_pickup" for item in findings)
    assert all(item["family_size"] == 3 for item in findings)
    assert all(item["in_family"] for item in findings)
    assert all(item["evidence"]["adjusted_alpha"] == THRESHOLDS.alpha / 3 for item in findings)
    assert all(item["causal"] is False for item in findings)
    assert "cohort_selection" in TESTING_FAMILIES
    assert len(TESTING_FAMILIES) == len(set(TESTING_FAMILIES))


def _phase1_call_agg(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE call_agg (
            workflow_id TEXT, config_version INTEGER, cohort_id TEXT, call_date TEXT, hour INTEGER,
            campaign_day_index INTEGER, lang TEXT, city_tier TEXT, age_band TEXT, value_band TEXT,
            welcome_message TEXT, voice TEXT, caller_id TEXT, prompt_version TEXT,
            dials INTEGER, connected INTEGER, engaged INTEGER, halluc_flags INTEGER,
            concluded_interested INTEGER, concluded_not_interested INTEGER,
            no_answer INTEGER, voicemail INTEGER, short_hangup INTEGER,
            PRIMARY KEY (
                workflow_id, config_version, cohort_id, call_date, hour, campaign_day_index,
                lang, city_tier, age_band, value_band, welcome_message, voice, caller_id, prompt_version
            )
        )"""
    )


def _phase1_row(conn: sqlite3.Connection, call_date: str, dials: int, connected: int, engaged: int) -> None:
    conn.execute(
        """INSERT INTO call_agg VALUES
           ('nw', 1, 'default', ?, 9, 0, 'hi', '1', '18-30', 'low', 'W1', 'V1', 'D1', 'P1',
            ?, ?, ?, 0, 0, 0, 0, 0, 0)""",
        (call_date, dials, connected, engaged),
    )


def test_phase1_rows_keep_their_counts_and_join_history_or_live(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    _phase1_call_agg(conn)
    _phase1_row(conn, "2026-12-20", 10, 4, 1)
    _phase1_row(conn, SIM_DAY_ZERO, 7, 2, 2)
    _phase1_row(conn, "2027-01-05", 3, 1, 0)
    conn.commit()
    conn.close()

    store = Store(path, "old")
    assert store.call_totals() == {"dials": 20, "connected": 7, "engaged": 3}
    history = period_counts(store, "nw", "history")
    live = period_counts(store, "nw", "live")
    assert (history["dials"], history["connected"], history["engaged"]) == (10, 4, 1)
    assert history["pickup_rate"] == 0.4
    assert history["engagement_rate"] == 0.25
    assert (live["dials"], live["connected"], live["engaged"]) == (10, 3, 2)
    assert live["pickup_rate"] == 0.3
    assert live["engagement_rate"] == 2 / 3
    kinds = {
        row["kind"]
        for row in store.conn.execute("SELECT DISTINCT kind FROM call_agg")
    }
    assert kinds == {"history", "events"}
    store.close()

    reopened = Store(path, "old")
    assert reopened.call_totals() == {"dials": 20, "connected": 7, "engaged": 3}
    assert period_counts(reopened, "nw", "history")["dials"] == 10
    assert period_counts(reopened, "nw", "live")["dials"] == 10
    assert reopened._table_exists("call_agg_v1") is False
    reopened.close()


def test_migration_resumes_after_rename_and_does_not_double_count(tmp_path):
    path = tmp_path / "partial.sqlite"
    conn = sqlite3.connect(path)
    _phase1_call_agg(conn)
    _phase1_row(conn, "2026-12-20", 10, 4, 1)
    conn.execute("ALTER TABLE call_agg RENAME TO call_agg_v1")
    conn.executescript(_CALL_AGG_DDL)
    conn.commit()
    conn.close()

    store = Store(path, "partial")
    assert store.call_totals() == {"dials": 10, "connected": 4, "engaged": 1}
    assert period_counts(store, "nw", "history")["dials"] == 10
    assert period_counts(store, "nw", "live")["dials"] == 0
    assert store._table_exists("call_agg_v1") is False
    store.close()


def test_finished_legacy_copy_is_reclassified_without_duplicating(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    _phase1_call_agg(conn)
    _phase1_row(conn, "2026-12-20", 10, 4, 1)
    conn.execute("ALTER TABLE call_agg RENAME TO call_agg_v1")
    conn.executescript(_CALL_AGG_DDL)
    conn.execute(
        """INSERT INTO call_agg (
               workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
               campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
               voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
               concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
           ) VALUES (
               'nw', 'legacy', '', 1, 'default', '2026-12-20', 9, 0, 'hi', '1', '18-30', 'low',
               'W1', 'V1', 'D1', 'P1', 10, 4, 1, 0, 0, 0, 0, 0, 0
           )"""
    )
    conn.commit()
    conn.close()

    store = Store(path, "legacy")
    assert store.call_totals() == {"dials": 10, "connected": 4, "engaged": 1}
    assert period_counts(store, "nw", "history")["pickup_rate"] == 0.4
    assert period_counts(store, "nw", "live")["dials"] == 0
    assert store._table_exists("call_agg_v1") is False
    store.close()


def test_divergent_migration_copies_are_left_in_place(tmp_path):
    path = tmp_path / "divergent.sqlite"
    conn = sqlite3.connect(path)
    _phase1_call_agg(conn)
    _phase1_row(conn, "2026-12-20", 10, 4, 1)
    conn.execute("ALTER TABLE call_agg RENAME TO call_agg_v1")
    conn.executescript(_CALL_AGG_DDL)
    conn.execute(
        """INSERT INTO call_agg (
               workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
               campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
               voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
               concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
           ) VALUES (
               'nw', 'history', '', 1, 'default', '2026-12-20', 9, 0, 'hi', '1', '18-30', 'low',
               'W1', 'V1', 'D1', 'P1', 1, 1, 0, 0, 0, 0, 0, 0, 0
           )"""
    )
    conn.commit()
    conn.close()

    try:
        Store(path, "divergent")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "different totals" in str(exc)
    assert raised
    conn = sqlite3.connect(path)
    old = conn.execute("SELECT SUM(dials) FROM call_agg_v1").fetchone()[0]
    new = conn.execute("SELECT SUM(dials) FROM call_agg").fetchone()[0]
    conn.close()
    assert old == 10
    assert new == 1


def _load_two_ages(store: Store) -> None:
    events = [{
        "type": "campaign_created",
        "ts": "2026-12-21T08:00:00+05:30",
        "tenant_id": "northwind_life",
        "workflow_id": "nw_renewal",
        "campaign_id": "nw_renewal:c0001",
        "leads": 2000,
        "config_version_at_creation": 1,
        "event_id": 1,
    }]
    event_id = 2
    for index, (age, connected_every) in enumerate((("18-30", 4), ("60+", 2))):
        for person in range(1000):
            lead_id = f"nw_renewal:l{index}{person:04d}"
            events.append({
                "type": "lead",
                "ts": "2026-12-21T08:00:00+05:30",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "lead_id": lead_id,
                "person_id": f"nw_renewal:p{index}{person:04d}",
                "features": {"lang": "hi", "city_tier": "1", "age_band": age, "value_band": "mid"},
                "event_id": event_id,
            })
            event_id += 1
            connected = person % connected_every == 0
            events.append({
                "type": "call",
                "ts": "2026-12-21T09:00:00+05:30",
                "tenant_id": "northwind_life",
                "workflow_id": "nw_renewal",
                "campaign_id": "nw_renewal:c0001",
                "lead_id": lead_id,
                "person_id": f"nw_renewal:p{index}{person:04d}",
                "attempt_no": 1,
                "campaign_day_index": 0,
                "config_version": 1,
                "cohort_id": "default",
                "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
                "connected": connected,
                "engaged": connected and person % 2 == 0,
                "disposition": "concluded_interested" if connected and person % 2 == 0 else (
                    "short_hangup" if connected else "no_answer"
                ),
                "qa": {"hallucination_flag": False, "flow_adherence": 0.9} if connected else None,
                "event_id": event_id,
            })
            event_id += 1
    store.apply_chunk({"chunk": 0, "until_ts": "2026-12-21T09:00:00+05:30"}, events, "history")
