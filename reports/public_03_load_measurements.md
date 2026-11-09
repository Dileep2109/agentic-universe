# public_03 load measurements

Partial run. The five-day scenario did not finish, and `sim grade` was not run.

## Requirement

Kabir Menon’s platform note asks the controller to ingest and decide on one simulated day of `public_03_load` in under 10 wall-clock minutes on 4 vCPU / 8 GB. The scenario is 12 workflows, 5 days, 15-minute ticks, end minute 7,200. This measurement was taken on the local Windows machine, not on a reserved 4 vCPU / 8 GB host.

## Command and isolation

- Simulator: `py -3 -m sim serve --scenario scenarios/public_03_load.yaml --run-dir C:\Users\smdil\AppData\Local\au-grade\public_03_run`
- Controller: `py -3 -m controller --url http://127.0.0.1:53123 --state-dir C:\Users\smdil\AppData\Local\au-grade\public_03_state`
- Database: `C:\Users\smdil\AppData\Local\au-grade\public_03_state\public_03_load.sqlite`
- Logs: `C:\Users\smdil\AppData\Local\au-grade\public_03_sim.log`, `public_03_controller.log`
- Runner: `C:\Users\smdil\AppData\Local\au-grade\run_public_03.py` (exit 3, time budget)

`reports/public_01.md`, `reports/public_02.md`, and the public_01 / public_02 logs and databases were not modified. No other simulator or controller was running. The controller used only the documented HTTP API.

A baseline generation was started and then stopped so it would not compete with the controller. `state/grade/public_03_baseline` is an incomplete world and must not be graded.

## Result

| Item | Measured |
|---|---|
| Runner exit | 3 (stopped on the 50-minute controller budget) |
| Controller | Terminated by the runner. Did not exit 0. |
| Simulator | Terminated by the runner. |
| `sim grade` | Not run |
| Boot | 126.8 s. History already in the simulator: 5,537,379 events. End minute 7,200. |
| Controller wall clock | 3,015 s to the stop |
| Simulated time reached | Minute 1,680 of 7,200 (1 day + 4 hours). `finished` false. |
| One simulated day (minute 0 to 1,440) | 34 min 29.8 s (23:06:06.684 to 23:40:36.437). Target is under 10 min. Not met. |
| History ingest | 5,537,379 events in 5 min 25.3 s, about 17,000 events/s |
| Live events in the completed day | 840,012 (6,377,391 − 5,537,379) |
| Live dials in the completed day | 648,000 calls, 156,911 connected, 85,397 engaged |
| History dials | 4,193,295 calls |
| Live leads / campaigns | 192,000 leads, 12 campaign_created |
| Events/s over the full simulated day | 406 |
| Events/s over 09:00–21:00 | 690 (840,012 events in 1,217 s) |
| Dials/s over 09:00–21:00 | 532 |
| Fastest 31 s sample | 6,157 events/s at 09:00 (192,012 events) |
| Credits | 400 remaining of 400. Consumed 0. |
| HTTP | 4,235 responses, all 2xx. Failures 0. Retries 0. Timeouts 0. Rejected writes 0. |
| Proposals / approvals / config changes | 0 / 0 / 0 |
| Audits | 112 `advance_confirmed`, 28 `opportunity_detected` |
| Controller peak memory | 1,686.8 MB |
| Simulator peak memory | 604.5 MB |
| Controller CPU at minute 1,440 | 2,268.8 CPU-seconds over 2,469 wall-seconds |

Decision lag, measured as the wall time between consecutive advances:

- Ordinary night tick: about 0.8 s
- Ordinary daytime tick: about 3.5–4 s
- Hourly snapshot tick: 52 s at 01:00, 55 s at 02:00, 74 s for the 09:00 hour, 84 s for the 10:00 hour, 134 s from 21:00 to 22:00

The hourly snapshot is what breaks the 10-minute day. It already costs one to two minutes and got slower as the store grew. A 10× volume would hit that snapshot first. No configuration was changed to chase the target.

## Checkpoint and recovery

The store has one checkpoint: scenario `public_03_load`, minute 1,680, event 6,377,391, no pending advance. That is the normal per-tick checkpoint. Crash recovery was not tested. The process was stopped on purpose and was not restarted.

## Why the five-day run was stopped

At the measured pace, five simulated days are about three hours plus history boot. The controller was stopped after 50 minutes so the partial numbers could be kept. Grade output would not describe the five-day scenario.

## Known public_02 issue, not fixed here

public_02 wrote `supervisor_modification_detected` 626 times for the same ks_cancel supervisor edit. That dedup was not changed before or during this run. This partial public_03 window did not contain a supervisor edit.
