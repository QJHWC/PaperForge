# PaperForge v3 architecture

## Runtime

`PaperForgeService` is the public facade. It persists workflows and events in
Scientific Memory, applies an `ExecutionPolicy`, then dispatches typed requests
through `ResearchOSRuntime` to Research, Experiment, Code, Compute, Analysis,
Visualization, Paper, Reviewer, and Release agents.

Each transition is versioned, idempotent, and resumable. Agent inputs and
outputs are stored as content-addressed traces. Missing prerequisites produce a
truthful `BLOCKED` result rather than a simulated success.

## Scientific Memory

The SQLite schema stores:

- source snapshots and licenses;
- evidence with commit, path, line range, excerpt, scope, capture time, and hash;
- claims and typed claim-evidence relations;
- experiment proposals and immutable run provenance;
- artifacts, reviews, workflows, events, and approvals.

Publication uses both the claim gate and exact LaTeX sentence coverage. A
caller-supplied Boolean cannot override the database.

## Profiles and approvals

The same policy instance controls context files, tool access, subprocesses,
remote execution, and workflow transitions. `full` workflows pause at
`AWAITING_APPROVAL` until the proposal is approved. Experiment evidence becomes
claim-eligible only after execution through the verified experiment path.

## Publication

The publication pipeline performs at most three compile-render-diagnose-repair
rounds. Repairs are limited to layout settings. It snapshots claims, citations,
numbers, bibliography, and protected experiment blocks before each repair and
rolls back any semantic change.

The final release verifier independently checks the Claim DB, publication
manifest, PDF hash, rendered page hashes, page-inspection record, source bundle,
source lock, protected invariants, and secret scan before allowing a workflow
to become `COMPLETED`.

## External sources

`third_party/source-lock.json` pins external repositories and documents whether
their content is vendored, user supplied, or reference only. Runtime publication
does not follow upstream branches.
