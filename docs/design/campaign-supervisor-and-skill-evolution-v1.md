# Campaign Supervisor and Skill Evolution v1

## Problem and non-goals

TaskFoundry can classify Harbor results, maintain Teacher leases, run persistent validation, and resolve a frozen Skill Bank activation. Those capabilities are not driven by one durable owner. A one-shot Harbor subprocess failure can therefore leave a run marked active without a retry budget, recovery condition, or automatic Skill attribution. Desktop task creation is also required before Harbor even though Harbor creates the actual Codex Researcher identity.

This change does not alter scientific packages, thresholds, verifier logic, existing evidence, or completed question families. It does not promise progress while an external provider is unavailable; it guarantees that such a wait is explicit, recoverable, and does not silently occupy an execution slot.

## Mode / scope / applicable rule overrides

- STRICT / L3, incremental.
- Temporary layers: campaign orchestration, Harbor adapter, Skill Bank governance.
- Each implementation slice stays within 300 effective production lines, six production files, and two layers.
- No waivers.

## Current flow

`Teacher task -> Desktop Researcher task -> capability -> one-shot execute_harbor -> manual retry/import -> optional attribution -> completion-only reconcile`

The scheduler controls Teacher leases but does not own Harbor recovery. `execute_harbor` returns one receipt. Model-transport failures are classified, but no shared circuit prevents many questions from retrying the same broken route. Skill attribution and candidate evaluation remain optional commands.

## Proposed flow

`CampaignSupervisor -> Teacher lease/decision -> runtime-owned Researcher request -> persistent Harbor -> canonical import -> next action`

The Supervisor has a small interface:

- `run_once()` advances every recoverable item exactly once.
- `run_forever()` repeats `run_once()` with a bounded poll interval.
- `resume(question_id)` clears one satisfied External Wait and requeues it.

Runtime attempts use the deterministic control identity `harbor-runtime:<validation-session-id>` for capability ownership. The authority-bearing Researcher identity remains the fresh Codex thread/session recorded from the Harbor sandbox. No ordinary Desktop Researcher task is created.

## Files, modules, and architecture layers

- `taskfoundry/recovery.py`: retry policy, shared model-transport circuit, durable recovery record.
- `taskfoundry/supervisor.py`: campaign orchestration and injected Harbor/Teacher adapters.
- `taskfoundry/researcher.py`: runtime-owned capability redemption and structured failure detail.
- `taskfoundry/scheduler.py` / `scheduler_state.py`: explicit External Wait recovery metadata and topic abandonment.
- `taskfoundry/skillbank.py`: mandatory revision attribution and batch terminal reconciliation/evaluation handoff.
- `taskfoundry/cli.py`: thin `supervise` and `resume-supervision` entry points.

## Public interface/schema/event/config changes

- Supervisor state schema v1 records question, revision, scientific round, runtime attempt, retry budget, next probe, last failure stage, and evidence path.
- Shared circuit schema v1 records route, state, failure count, opened time, next probe, and last evidence digest. It never records credentials or proxy values.
- Scheduler External Wait gains recovery condition, next probe, and evidence path.
- A runtime-owned Researcher request is explicitly marked and can be redeemed only by the Supervisor with the same deterministic identity.
- Revision terminal events require one Teacher Skill attribution decision, including an explicit non-evolvable result.

## Compatibility and migration

Existing schema-v1/v2/v3 Researcher requests and scheduler snapshots remain readable. Existing Desktop-bound requests retain their old redemption rule. Runtime-owned requests are opt-in and use a new request schema. Existing runs are not rewritten; the Supervisor derives recovery state from canonical evidence and appends its own records.

## Transaction/concurrency/security model

- One filesystem lock protects Supervisor state and one protects the shared circuit.
- A runtime attempt identity is immutable and idempotent; a failed runtime retry uses a new request/job/trial identity but the same scientific round.
- Scientific package bytes, verifier bytes, thresholds, and Skill activation bytes remain immutable during a validation session.
- `.env` is passed by path to Harbor and never serialized into Supervisor state, logs, or evidence.
- The Supervisor cannot mark a scientific result without canonical Harbor evidence.

## Error taxonomy and recovery boundaries

- IMAGE, SANDBOX, PROVIDER: retry the same Scientific Round at 30, 120, and 300 seconds; after three failures enter External Wait.
- MODEL_CONNECTION: run one fixed probe. Failure opens a shared 15-minute circuit and places all dependent questions in External Wait. A successful probe closes the circuit and resumes them.
- HARNESS_BOOTSTRAP, VERIFIER, EXECUTION_CONTRACT_FAILURE: deterministic Teacher audit; do not retry unchanged bytes.
- Scientific timeout/result: use the validation decision contract; never treat it as platform recovery.
- A platform failure cannot create Skill Bank evidence. A scientific redesign or shortcut failure must create a Teacher attribution decision.

## Consumer/import graph impact

`taskfoundry.cli`, scheduler workers, and tests consume the new Supervisor interface. Existing direct `researcher-run` remains compatible. Workflow and evidence importers keep authority over scientific state transitions.

## Implementation slices

1. Recovery policy and circuit: `recovery.py`, scheduler state integration, unit tests.
2. Runtime-owned Researcher launch: researcher/workflow/CLI changes and compatibility tests.
3. Supervisor orchestration: supervisor module, thin CLI, integration tests with fake adapters.
4. Skill closure: mandatory terminal attribution, reconcile/evaluate coordinator, scheduler integration tests.
5. Q10/Q12 dry-run migration and rollback verification without changing scientific bytes.

## Unit, branch, integration, and coverage plan

- Retry schedule, exhaustion, circuit open/half-open/close, concurrent lock, restart recovery.
- Runtime-owned redemption, Desktop-bound compatibility, duplicate request rejection, immutable package binding.
- Crash after launch/before receipt, after receipt/before import, and after import/before scheduling.
- Model outage shared by two questions creates one probe and two External Waits.
- Platform failures generate no attribution; scientific terminal outcomes require one.
- Batch with zero, one, and two matching evolvable attributions; candidate evaluation passes/fails all three gates.
- Integration path: fake Harbor fails model transport, circuit opens, clock advances, probe succeeds, same Scientific Round resumes and imports one result.
- Full TaskFoundry tests, structural checks, diff coverage when available; otherwise branch-to-test mapping.

## Observability and sensitive-data policy

Record question/revision/session/request/job/trial/sandbox/session identities, classifications, failure stage, retry decision, timestamps, evidence digests, circuit transitions, and Skill attribution outcomes. Never record credential values, proxy URLs containing credentials, private GT, grader bytes, or hidden labels.

## Rollback and data-recovery boundary

- Code rollback target: Git tag `backup/pre-campaign-supervisor-20260831`.
- Runtime rollback snapshot: ignored `archive/pre-campaign-supervisor-20260831/` contains Q10, Q12, and scheduler state.
- Removing the `supervise` process leaves legacy direct commands usable.
- Supervisor records are append-only or reconstructible; rollback never deletes canonical Harbor evidence.

## Waivers, approvers, and expiry

None.

## Open decisions that block implementation

None. The user confirmed the Supervisor owner, runtime-owned Researcher identity, retry/circuit policy, explicit terminal states, mandatory Skill closure, thin CLI, and Q10/Q12 acceptance paths.
