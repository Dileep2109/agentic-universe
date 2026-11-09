# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.231 | -6.6% | 0.179 | -2.5% | 0.460 | -3.4% | 0% | 323 (330) |
| nw_kyc | 0.367 | -2.2% | 0.237 | -2.1% | 0.593 | +0.1% | 0% | 28 (31) |
| nw_renewal | 0.425 | -0.3% | 0.260 | -0.6% | 0.550 | +0.3% | 5% | 46 (55) |
| ALL | 0.325 | -2.9% | 0.218 | -1.7% | 0.525 | -0.9% | 3% | 397 (416) |

## Changes applied by the controller

2 applied — beneficial: 1, harmful: 1

- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.193 → **beneficial**
- ks_cancel v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.159 → **harmful**

## Governance

```
{
  "approval_requests": 0,
  "decisions": {
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 0,
  "auto_applied": 2,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {}
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.468,
      "3": 0.498,
      "4": 0.498,
      "5": 0.507,
      "6": 0.511,
      "7": 0.502,
      "8": 0.498,
      "9": 0.491
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": null,
    "fixed_by": null
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0