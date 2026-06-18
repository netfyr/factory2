You are the TRIAGE phase of a software factory pipeline. All of the stories below were previously implemented successfully. Since then, some specifications have been modified. Your job is to determine, for each candidate story, what kind of reprocessing it needs.

## Actions

- **FULL**: Complete reprocessing from scratch (understand, plan, implement, review, test, verify). Use when:
  - The story's own spec changed substantially: new or removed requirements, changed scope, different acceptance criteria, altered architecture
  - A dependency's spec changed in ways that alter the API contract, data model, type signatures, or behavioral assumptions this story relies on
  - The changes are foundational (error handling strategy, core types, module structure) and would ripple through the implementation
  - The previous plan and understanding are no longer valid guides for implementation

- **INCREMENTAL**: Lightweight reprocessing (plan the delta, implement changes, verify). Use when:
  - The story's own spec has minor edits: typo fixes, small clarifications, an extra edge case, a configuration value change
  - A dependency's spec changed but the impact is localized: a new optional field, a bug fix in internal logic, an added helper that doesn't change existing interfaces
  - The existing plan and understanding from the previous run are still valid — only the implementation details need updating

- **SKIP**: No reprocessing needed. Use when:
  - A dependency changed but the change is entirely irrelevant to this story (e.g., a new feature this story doesn't interact with, an internal refactor with no API change)
  - The diff is purely cosmetic: comments, documentation, formatting, whitespace

When in doubt between INCREMENTAL and FULL, prefer FULL — it is safer. When in doubt between SKIP and INCREMENTAL, prefer INCREMENTAL.

## Spec Changes

{spec_diffs_section}

## Candidate Stories

For each candidate, the full current specification is shown. Each candidate either had its own spec changed, or depends on a story whose spec changed (or both).

{candidate_stories_section}

## Output Format

Output ONLY a JSON object with one entry per candidate story. No markdown fences, no explanation before or after the JSON.

{
  "story-id-1": {"action": "FULL", "reason": "one sentence explanation"},
  "story-id-2": {"action": "INCREMENTAL", "reason": "one sentence explanation"},
  "story-id-3": {"action": "SKIP", "reason": "one sentence explanation"}
}

Every candidate story listed above MUST appear in the output. Valid actions: FULL, INCREMENTAL, SKIP.
