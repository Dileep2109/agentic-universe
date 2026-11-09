"""Workflow snapshot for a supervisor.

The snapshot is computed from this controller's database. Publishing it does
not call the simulator and does not insert a proposal.
"""

from __future__ import annotations

from .analytics import (
    call_groups,
    campaign_counts,
    counts_for_features,
    feature_levels,
    period_counts,
    upload_day_counts,
)
from .cohorts import COMPLETE_FEATURE_DENOMINATOR, build_cohort_set
from .monitor import (
    COHORT_CONTRAST_GUARDRAIL_FAMILY,
    COHORT_CONTRAST_PRIMARY_FAMILY,
    HOUR_CONTRAST_FAMILY,
    LIVE_VS_HISTORY_GUARDRAIL_FAMILY,
    LIVE_VS_HISTORY_KPI_FAMILY,
    OBSERVATIONAL_WARNING,
    assess_change,
    assess_guardrail,
    assess_slices,
)
from .settings import RunSettings
from .stats import bonferroni
from .store import Store


def publish_snapshot(store: Store, settings: RunSettings | None = None) -> dict:
    settings = settings or RunSettings()
    workflows = []
    for document in store.workflow_documents():
        workflows.append(_workflow_snapshot(store, document, settings))
    document = {
        "scenario": store.scenario,
        "sim_minute": store.sim_minute,
        "sim_ts": store.sim_ts,
        "kind": "performance_snapshot",
        "incidents": [],
        "incident_note": (
            "A confirmed finding is a statistical difference under the configured thresholds. "
            "It is not a causal estimate and it is not an incident. "
            "Phase 2 does not open incidents and it does not submit proposals."
        ),
        "workflows": workflows,
    }
    store.save_snapshot(document)
    return document


def _workflow_snapshot(store: Store, workflow: dict, settings: RunSettings) -> dict:
    workflow_id = workflow["workflow_id"]
    objective = workflow.get("objective") or {}
    primary = objective.get("primary")
    history = period_counts(store, workflow_id, "history")
    live = period_counts(store, workflow_id, "live")
    overall = period_counts(store, workflow_id, "all")
    for period, counts in ("history", history), ("live", live), ("all", overall):
        store.replace_baseline(workflow_id, period, counts)

    period = "history" if (history.get("leads_uploaded") or 0) > 0 else "all"
    levels, total_leads = feature_levels(store, workflow_id, period)
    cohort_set, cohort_note = build_cohort_set(
        levels,
        total_leads,
        primary_metric=primary or "",
        policy=settings.cohorts,
        thresholds=settings.thresholds,
    )
    store.replace_cohorts(
        workflow_id,
        [cohort.as_dict(index) for index, cohort in enumerate(cohort_set.cohorts)],
        period,
        selection_evidence=cohort_set.selection,
    )
    stored = store.cohort_rows(workflow_id)
    if [row["cohort_id"] for row in stored] != [cohort.id for cohort in cohort_set.cohorts]:
        raise RuntimeError(f"stored cohorts for {workflow_id} do not match the set just built")

    cohort_counts = counts_for_features(store, workflow_id, cohort_set, kind="history" if period == "history" else None)
    cohort_findings, missing_features = _cohort_findings(cohort_counts, primary, objective, settings)
    hours = _with_totals(call_groups(store, workflow_id, "hour", kind="history" if period == "history" else None), "hour")
    hour_findings = assess_slices(
        hours,
        "pickup_rate",
        settings.thresholds,
        subject_key="hour",
        family=HOUR_CONTRAST_FAMILY,
    )
    versions = call_groups(store, workflow_id, "config_version")
    findings = _period_findings(history, live, primary, objective, settings) + cohort_findings + hour_findings
    return {
        "workflow_id": workflow_id,
        "tenant_id": workflow.get("tenant_id"),
        "objective": objective,
        "baselines": {"history": history, "live": live, "all": overall},
        "versus_history": [item for item in findings if item["subject"] == "live_vs_history"],
        "guardrail": next((item for item in findings if item["role"] == "guardrail" and item["subject"] == "live_vs_history"), None),
        "shared_cohorts": {
            "note": cohort_note,
            "source_period": period,
            "selection_evidence": cohort_set.selection,
            "definitions": stored,
            "performance": cohort_counts,
            "missing_features": missing_features,
        },
        "feature_slices": {
            feature: call_groups(store, workflow_id, feature, kind="history" if period == "history" else None)
            for feature in ("lang", "city_tier", "age_band", "value_band")
        },
        "hours": hours,
        "hour_findings": hour_findings,
        "config_versions": versions,
        "campaigns": campaign_counts(store, workflow_id),
        "upload_days": upload_day_counts(store, workflow_id),
        "confirmed": [item for item in findings if item["status"] == "confirmed"],
        "observations": [item for item in findings if item["status"] in ("observation", "insufficient")],
        "within_variation": [item for item in findings if item["status"] == "within_variation"],
    }


def _period_findings(history: dict, live: dict, primary: str | None, objective: dict, settings: RunSettings) -> list[dict]:
    metrics = ("pickup_rate", "engagement_rate", "business_outcome")
    adjusted = bonferroni(settings.thresholds.alpha, len(metrics))
    findings = []
    for metric in metrics:
        finding = assess_change(
            history,
            live,
            metric,
            settings.thresholds,
            adjusted_alpha=adjusted,
            subject="live_vs_history",
            family=LIVE_VS_HISTORY_KPI_FAMILY,
            family_size=len(metrics),
            observational_warning=OBSERVATIONAL_WARNING,
        )
        finding["role"] = "primary" if metric == primary else "secondary"
        findings.append(finding)
    guardrail = assess_guardrail(objective, history, live, settings.thresholds, subject="live_vs_history")
    if guardrail is not None:
        guardrail["family"] = LIVE_VS_HISTORY_GUARDRAIL_FAMILY
        guardrail["family_size"] = 1
        guardrail["observational_warning"] = OBSERVATIONAL_WARNING
        findings.append(guardrail)
    return findings


def _cohort_findings(
    counts: dict[str, dict], primary: str | None, objective: dict, settings: RunSettings
) -> tuple[list[dict], dict]:
    """Compare complete-feature cohorts. The missing bucket is reported, not tested."""
    missing = counts.get("missing") or {"dials": 0, "connected": 0, "engaged": 0}
    missing_report = {
        "policy": COMPLETE_FEATURE_DENOMINATOR,
        "dials": int(missing.get("dials") or 0),
        "connected": int(missing.get("connected") or 0),
        "engaged": int(missing.get("engaged") or 0),
    }
    if not primary or primary not in ("pickup_rate", "engagement_rate", "business_outcome"):
        return [], missing_report
    known = {cohort_id: item for cohort_id, item in counts.items() if cohort_id != "missing"}
    dials = sum(int(item["dials"]) for item in known.values())
    connected = sum(int(item["connected"]) for item in known.values())
    engaged = sum(int(item["engaged"]) for item in known.values())
    slices = []
    for cohort_id, item in known.items():
        slices.append({
            "cohort": cohort_id,
            "dials": item["dials"],
            "connected": item["connected"],
            "engaged": item["engaged"],
            "_total_dials": dials,
            "_total_connected": connected,
            "_total_engaged": engaged,
        })
    primary_findings = assess_slices(
        slices,
        primary,
        settings.thresholds,
        subject_key="cohort",
        family=COHORT_CONTRAST_PRIMARY_FAMILY,
    )
    for finding in primary_findings:
        finding["role"] = "primary"
        _attach_denominator(finding, missing_report)
    guard_metric = _guardrail_metric(objective)
    if guard_metric is None or guard_metric == primary:
        return primary_findings, missing_report
    guard_findings = assess_slices(
        slices,
        guard_metric,
        settings.thresholds,
        subject_key="cohort",
        family=COHORT_CONTRAST_GUARDRAIL_FAMILY,
        family_alpha=settings.thresholds.guardrail_alpha(),
    )
    for finding in guard_findings:
        finding["role"] = "guardrail"
        _attach_denominator(finding, missing_report)
    return primary_findings + guard_findings, missing_report


def _attach_denominator(finding: dict, missing_report: dict) -> None:
    finding["denominator_policy"] = missing_report["policy"]
    finding["missing_excluded"] = {
        "dials": missing_report["dials"],
        "connected": missing_report["connected"],
        "engaged": missing_report["engaged"],
    }


def _with_totals(rows: list[dict], key: str) -> list[dict]:
    dials = sum(row["dials"] for row in rows)
    connected = sum(row["connected"] for row in rows)
    engaged = sum(row["engaged"] for row in rows)
    out = []
    for row in rows:
        item = dict(row)
        item[key] = row["key"]
        item["_total_dials"] = dials
        item["_total_connected"] = connected
        item["_total_engaged"] = engaged
        out.append(item)
    return out


def _guardrail_metric(objective: dict | None) -> str | None:
    guard = (objective or {}).get("guardrail") or {}
    if "pickup_rate_max_drop" in guard:
        return "pickup_rate"
    if "engagement_rate_max_drop" in guard:
        return "engagement_rate"
    return None
