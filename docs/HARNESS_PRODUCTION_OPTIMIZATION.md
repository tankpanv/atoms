# Harness production execution model

## Observed failure mode

The 2026-10-06 StyleMate run spent roughly 21 minutes in model calls: 9.4 minutes planning/recovery, 7.6 minutes implementation, and 4.4 minutes context compaction. It entered the test stage 38 times and reused the same successful build 36 times. The backend task remained `in_progress`, so the frontend task never started and the generated entry page remained a template.

The expensive loop was:

```
read or invalid verification -> recovery diagnosis -> checkpoint_due
-> source inspection -> cached build -> completion gap -> implementation
```

A recovery diagnosis and a cached result are observations. Neither is a code or task transition. Treating them as acceptance boundaries caused repeated model turns without new evidence.

## Execution rules

- A recovery diagnosis sends the next model turn a new hypothesis. It does not schedule build, startup, or completion acceptance.
- A successful command/browser receipt is reusable for the same source, runtime configuration, and action fingerprint.
- A cached receipt does not schedule another acceptance pass.
- Acceptance is scheduled after a source/configuration mutation, a task transition to `done` or `deferred`, or entry into bounded delivery stabilization.
- An `in_progress` note, read-only tool, context compaction, or repeated status query cannot schedule acceptance.
- Planning has one synthesis call and at most one JSON repair call. It uses a compact output budget; implementation receives the saved plan rather than re-planning.
- External dependency failures are task-local TODOs. Independent work continues; a simulated adapter is allowed only when visibly labelled and does not satisfy real-provider evidence.

This follows the useful parts of OpenCode/OpenHands: action-level progress, durable receipts, bounded retry, and rolling context. It intentionally does not import their interactive session behavior into a long-running build job.
