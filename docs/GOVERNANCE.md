# Governance

The controller changes a workflow only through the documented simulator routes. Policy numbers are read from the workflow document (`GET /workflows` and `GET /workflows/{id}`). They are not constants in the decision code.

## Tick order

`controller/loop.py` does this on every pass, including the pass that stops because `--max-advances` is already satisfied:

1. Read `GET /sim/state`.
2. A pending advance is reconciled before the loop, from that state, and is not sent again.
3. Ingest chunks that are already available.
4. Refresh workflow documents and the current configuration.
5. Publish or update the supervisor snapshot when it is due: the first pass, the pass that stops the loop, the next pass after a configuration write, and whenever the simulated hour changes. Passes in between keep the previous snapshot. Governance does not read that document.
6. Review experiment designs. They stay non-executable. `causal` stays false.
7. Reconcile proposals and approvals, detect a caller-id opportunity only from the calls just stored, and submit or apply only when `evaluate_policy` allows that exact diff.
8. Write the tick metric.
9. Advance 15 minutes, unless the scenario is finished or the advance limit is already reached.

`--max-advances 0` ingests and runs one decision pass. It does not call `POST /sim/advance`.

## Endpoints used

Reads:

- `GET /sim/state`
- `GET /workflows`
- `GET /workflows/{id}/config`
- `GET /workflows/{id}/config/history`
- `GET /approvals`
- `GET /approvals/{id}`

Writes, each sent once and reconciled if the response is lost:

- `POST /approvals`
- `POST /workflows/{id}/apply` with `{"approval_id": "apr_..."}`
- `PUT /workflows/{id}/config` only when the fetched policy marks that exact change auto-apply eligible

`POST /sim/advance` is unchanged and still has no idempotency key. A lost advance is checked with `GET /sim/state` before another advance.

## Policy decision

`evaluate_policy` returns one of `rejected`, `approval_required`, or `auto_apply_eligible`.

It rejects the change when the workflow document is missing, governance fields are missing, the workflow is explicitly inactive, the configuration version used as evidence is not the current version, a supervisor edit is flagged on that snapshot, an option is not in the workflow's list, nothing changed, a prompt change has no `validation_run_id`, `mid_flight` is missing or a locked dimension is changed while `mid_flight` is `next_campaign_only`, another approval is pending, the recent request count is unknown or already at `max_requests_per_24h`, or the proposal is older than `proposal_ttl_hours`.

Auto-apply requires `auto_apply.enabled`, every changed dimension in `allowed_dims`, and total variation at most `max_shift`. Total variation is `0.5 * L1` on welcome, voice, and caller-id weights, taking the worst cohort. A shift equal to `max_shift` is eligible. A larger shift is not auto-applied. If the values are otherwise legal it is sent for approval instead. That does not widen `max_shift`.

Approver hours apply to the approval route. Outside those hours the proposal is stored as `drafted` and is not posted. Auto-apply does not wait for approver hours, because the simulator does not apply that window to `PUT /workflows/{id}/config`.

`next_campaign_only` locks `cohorts`, `welcome_message`, `voice`, and `caller_id` to the version from campaign creation. Those dimensions are rejected rather than submitted. `prompt_version` and `schedule` can still be proposed. The simulator samples each dial independently. A planned experiment arm is not exposure, and this phase does not claim a causal effect.

## What becomes a proposal

The only automatic change is a caller-id weight move. It is created only when live calls (not history) are all on the current configuration version, two caller ids each have at least the configured pickup sample (800 dials) and engagement sample (400 connected), the lower-pickup id is a confirmed difference at the configured alpha and minimum absolute gap, and the higher-pickup id is not itself worse on engagement by that same standard. The weight moved is at most the workflow `max_shift`. The impact record stores the observed gap. `expected_effect` stays null. Cohort contrasts, history-versus-live comparisons, and prompt summaries do not create a proposal.

If that bar is not met, the controller writes nothing.

## Lifecycle

1. The proposal row is stored before the HTTP write.
2. `POST /approvals` or `PUT /workflows/{id}/config` is sent once.
3. A lost response or a duplicate id is resolved with `GET /approvals` and configuration history. The same body is posted again only after those reads succeed and show that the id was not created.
4. An approved row is applied with `POST /workflows/{id}/apply` only while the approval status is `approved`, the base version still matches, and the actor is not a later supervisor.
5. After either write, the controller reads the configuration and its history. Success is recorded only when the version is the expected next version, the history row carries this proposal id, and the live weights match the proposal. Otherwise the outcome is `uncertain` or `failed`.

Simulator statuses used: `pending`, `approved`, `rejected`, `expired`, `applied`, `lapsed`. A lapsed or rejected approval is not applied again.

## Verification

After an auto-apply or an approved apply, the controller reads `GET /workflows/{id}/config` and `GET /workflows/{id}/config/history`. A row is `verified` only when all of these hold:

- The live version is the expected next version.
- History contains this proposal id at that version.
- Every changed dimension matches the proposal: caller id, welcome, and voice weights within `1e-6`, and schedule, prompt version, and cohorts exactly.
- No other dimension differs. A mismatch is `uncertain` with outcome `verification_mismatch`, not a successful apply.

Total variation is not the check. It ignores schedule, prompt, and cohorts.

## After application

The proposal stores the pre-change dials, connected calls, and engaged calls for that configuration version. It does not store a business-outcome baseline. Call aggregates do not identify unique concluded leads, so business outcome is `unavailable` and cannot trigger a rollback.

On a later tick the controller compares live calls on the applied version with that baseline:

- Primary KPI from the workflow objective. Engagement is engaged/connected. Pickup is connected/dials. The comparison uses the configured alpha, minimum absolute gap, and sample floor. A confirmed drop is primary degradation. A non-significant move is normal variation. A short sample is insufficient.
- Guardrail from the same objective, using the named maximum drop and `1 - guardrail_confidence`. A confirmed excess drop is a guardrail breach.

Both results are observational. `causal` stays false.

A confirmed primary drop or guardrail breach stores a restore proposal for the previous configuration and sends it through `evaluate_policy`. Auto-apply is used only when that restore is eligible. Otherwise the audit action is `rollback_recommended` and nothing is written. A supervisor actor on the current version blocks the restore and is recorded as `supervisor_modification_detected`. The restored configuration is verified with the same dimension check.

## Audit

`audit_log` is insert-only. Governance actions include `opportunity_detected`, `proposal_created`, `policy_rejected`, `approval_submitted`, `approval_approved`, `approval_rejected`, `approval_expired`, `auto_apply_attempted`, `configuration_applied`, `configuration_verification_failed`, `supervisor_modification_detected`, `degradation_detected`, `rollback_recommended`, and `rollback_applied`.

`opportunity_detected` is written when the decision changes: actionability, reason, pickup verdict, engagement verdict, or the caller pair. A repeated identical observation updates the stored evidence with the latest counts and comparison and keeps the first `detected_at`. It does not add another audit row. A later confirmed, actionable result is audited and still goes through proposal creation. Existing databases need no new column; the next observation replaces `evidence_json` on the same primary key.

Local tables added for this phase: `governance_opportunities`, `application_attempts`, and extra columns on `proposals` (`route`, `expires_at`, `policy_json`, `verification_json`). Existing databases gain the columns with `ALTER TABLE`.

## Public scenario matrix

These values were read back from a running `public_01` simulator in `tests/controller/test_governance.py`. Hidden scenarios are read the same way at runtime. Do not copy this table into the policy function.

| Workflow | Auto-apply | Dimensions the server allows on that path | Max shift | Everything else |
|---|---|---|---|---|
| `nw_renewal` | enabled | `schedule`, `caller_id` | 0.50 | Approval, if the value is legal. Prompt changes also need a validation run id. |
| `nw_kyc` | disabled | none | n/a | Approval. Caller id, welcome, voice, and cohorts are locked while `mid_flight` is `next_campaign_only`. |
| `ks_cancel` | enabled | `schedule`, `caller_id`, `welcome_message`, `voice` | 0.20 | Approval for a prompt, a cohort change, or a shift above 0.20. |

A customer-facing change on Northwind (welcome, voice, prompt) is not auto-applied. Sales' claim that KYC auto-apply is on does not match this document: `nw_kyc` has auto-apply disabled.
