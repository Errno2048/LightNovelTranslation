# Pipelined Translation Plan

## Goal

Add a new translation mode, `pipelined`, that allows batch translation to advance
before the preceding batch finishes review:

- Batch N is translated.
- Its drafts are written to the store.
- A provisional index block (summary + rolling term registry + draft chapter
  artifacts) is materialized.
- Batch N+1 translation starts immediately using those provisional artifacts.
- Batch N review may still be running.
- Reviews and acceptance can lag behind translation; acceptance remains ordered
  so the contiguous accepted prefix and export semantics stay intact.

The mode must preserve terminology consistency for no-reference terms across
batches. A shared rolling term registry makes first-seen draft targets
provisional canonicals, and later batches must use them. Review receives prior
unreviewed drafts as evidence and can detect or repair inconsistencies.

## Current blockers

1. `run_concurrent` is a sequential loop: translate batch -> review all ->
   accept all -> next batch.
2. `documents()` merges only accepted translations plus an explicit
   provisional map. Prior unreviewed batches are not passed.
3. `snapshot()` only materializes accepted target summaries. There is no
   provisional target summary/block scope.
4. `_history_pairs()`, `read_chapter`, and `lookup_term` only see accepted or
   reference text. Unreviewed drafts of prior batches are invisible to review.
5. Batch glossary extraction does not see prior drafts. New no-reference terms
   are confidence 3 candidates; `_build_term_checks` ignores confidence 3, so
   two batches can choose different targets and both pass review.
6. `put_glossary()` first-wins between equal-confidence candidates but does not
   make the winner mandatory; aliases still allow divergence.
7. Acceptance and target summaries are accepted-prefix driven.

## Design

### Batch states

```text
queued
  -> translating
  -> translated        drafts written
  -> indexed           provisional index block materialized
  -> reviewing
  -> reviewed
  -> accepted | blocked
```

Batch N+1 translation may start when every index block whose `end_order` is
<= batch N's end is materialized. Review state is not a dependency.

### Index block

An index block is materialized from drafts, not accepted text:

- provisional target summary for the batch range;
- provisional chapter artifacts for draft chapters;
- rolling term registry snapshot (provisional canonicals + used forms);
- optional entity/thread/evidence artifacts.

Store provisional summaries in the existing `summaries` table under scope
`target-provisional`. Accepted target summaries remain under `target`.
`snapshot()` prefers accepted summaries and adds provisional summaries only for
uncovered ranges.

### Rolling term registry

Extend the glossary with provisional state:

- Add `glossary.provisional INTEGER NOT NULL DEFAULT 0`.
- `GlossaryTerm` gains `provisional`.
- In pipelined mode, batch glossary extraction promotes newly proposed terms
  (confidence 3, no reference support) to:
  - `confidence=1`
  - `provisional=1`
  - `locked=0`
- The translation prompt treats confidence=1, locked, or provisional as
  mandatory target forms.
- Deterministic `_build_term_checks` already covers confidence<=2, so
  provisional confidence-1 terms are checked.
- Batch N+1 glossary extraction sees the provisional term and must reuse it.
- If a later draft uses a different form, the term check produces a
  `fixed_missing` finding; adjudication and revision repair it.

First-seen-wins is the default consistency policy. A later global term
adjudication can change a provisional canonical; the update must mark affected
chapters for revision. Implement the update/mark primitive now; automatic
global adjudication can be layered on top.

### Provisional context for translation

For batch N+1 translation:

- `documents()` receives a `provisional` map containing prior unreviewed
  drafts (not accepted).
- `snapshot()` receives `provisional_orders` and includes:
  - provisional chapter artifacts;
  - provisional summary blocks.
- The prompt marks provisional documents as `未审草稿` and provisional blocks
  with status `provisional`.
- Existing cache-friendly ordering is preserved: stable glossary/rules first,
  dynamic provisional context last.

### Review evidence

Review of batch N+1 must see prior unreviewed drafts:

- `_history_pairs(order, provisional=prior_drafts)` includes
  `authority="provisional_target"` pairs.
- `tool_context` gains `draft_texts`.
- `read_chapter` returns `language="zh-provisional"` for draft orders.
- `lookup_term` searches provisional drafts and marks hits with
  `authority="provisional_draft"`.
- Term checks include provisional canonicals; cross-batch inconsistency
  becomes a deterministic term finding.

### Scheduler

Use bounded pools:

- translation pool: per-batch chapter translation (warm last chapter first as
  in `run_concurrent`);
- index task: provisional summary + term registry materialization;
- review pool: review/adjudicate/revise per chapter;
- main thread: sole SQLite writer for drafts, index markers, and acceptance.

Loop outline:

1. Translate the next batch.
2. Write drafts; promote new batch glossary terms to provisional canonicals.
3. Materialize the provisional summary/index block.
4. Submit review jobs for the batch; do not wait.
5. If the review backlog is below the bound, continue with the next batch.
6. Drain completed review batches in order and accept them.
7. On any blocked batch, stop scheduling new translations, drain in-flight
   reviews/acceptance up to the block, and raise.
8. After all batches: drain all reviews; optionally refresh accepted target
   summaries.

Acceptance remains ordered. Review results may complete out of order but are
buffered per batch until the prior batch is accepted.

## Implementation steps

1. Plan file and task list.
2. Config: `translation_mode = "batch"|"pipelined"`,
   `pipeline_review_backlog`; validation and user-config allowlist.
3. Store/schema:
   - glossary `provisional` column and migration;
   - `draft_translations()`;
   - glossary read/write/confirm support for provisional;
   - provisional summary helpers.
4. Data model: `GlossaryTerm.provisional`, entity cards, translation prompt
   mandatory-form rule, term checks.
5. Provisional index:
   - `materialize_provisional_index(batch, drafts)`;
   - provisional summary generation + fallback;
   - `snapshot()` provisional summary blocks and draft chapter artifacts.
6. Provisional review context:
   - prior draft history;
   - `tool_context.draft_texts`;
   - `read_chapter`/`lookup_term` provisional access.
7. Scheduler: `run_pipelined` with bounded review backlog and ordered
   acceptance; factor reusable acceptance helpers from `run_concurrent`.
8. Mode registration: `run_mode` dispatcher, docs, skill operations/config
   example.
9. Tests:
   - config validation;
   - provisional glossary promotion and term check;
   - provisional summary/snapshot;
   - review tool provisional reads;
   - pipelined integration: batch N review lag, batch N+1 uses prior draft,
     ordered acceptance, backlog bound, blocked-batch policy.
10. Full test suite and docs sync.

## Failure and consistency policy

- Provisional first-seen canonical wins by default.
- A later term change must update the glossary and mark chapters using the old
  form for revision; the next pipeline run repairs them.
- A blocked batch stops new translation scheduling. Already-translated later
  batches remain drafts and must be re-reviewed/re-translated after the block
  is resolved if their provisional index depended on the blocked batch.
- Export requires all chapters accepted; provisional summaries are context
  only and never exported as accepted content.

## Non-goals for this iteration

- Fully automatic semantic term adjudication across all batches.
- Unbounded out-of-order acceptance/export.
- Replacing accepted summaries with provisional summaries in final exports.
- Optimizing provider prefix-cache behavior across arbitrary pipeline depth.

## Open risks

- Provisional first-seen canonicals may lock in a bad early translation;
  global term revision is required to repair it.
- Provisional summaries cost one model call per batch and may be revised after
  review.
- Concurrent review backlog increases memory, model usage, and log volume.
- Prefix-cache hit rate may fall as provisional context grows; measure after
  the mode works correctly.

## Implementation status

- [x] `translation_mode = "batch"|"pipelined"` and
  `pipeline_review_backlog` config/validation/allowlist.
- [x] Glossary `provisional` column, migration, merge policy, promotion, and
  `draft_translations()`.
- [x] `GlossaryTerm.provisional`, entity cards, mandatory-form prompt, term
  checks, adjudication/revision evidence.
- [x] `target-provisional` summaries, snapshot block artifacts, provisional
  chapter artifacts, and `materialize_provisional_index`.
- [x] Review history and `read_chapter`/`lookup_term` access to prior drafts.
- [x] `run_pipelined` with bounded review backlog, ordered acceptance,
  blocked-batch stop/drain, and `run_mode` dispatch.
- [x] Integration tests: review lag, prior-draft context, provisional term
  consistency, ordered acceptance, config dispatch.
- [x] Docs, config example, skill copy sync.
- [ ] Automatic global term adjudication and automatic revision re-scheduling;
  `mark_term_revisions` currently records affected chapters for the caller.
- [ ] Prefix-cache/cost measurement on a real long pipeline.

## Round 2: entity/form terms, title patterns, strict manual review

### Entity/form term registry

The current source-keyed glossary cannot distinguish a full name from a short
name with its own target. Replace the flat `(source,target,aliases)` model with
a two-table, one-level union-find-like structure:

- `term_entities(entity_id, label, kind, status, confidence, locked,
  provenance, ...)` is the stable entity root.
- `term_forms(source, entity_id, target, form_type, target_aliases,
  confidence, provisional, locked, evidence_count, first_seen, ...)` is a
  member form.

Rules:

- `source` is unique; `entity_id` is an explicit root (no parent chains,
  no path compression in hot paths).
- Each form has its own target. The entity canonical is for display/review
  only and never overrides a form target.
- `target_aliases` are alternate Chinese forms for that form; other source
  forms are separate rows sharing `entity_id`.
- A new no-reference form may attach to an existing entity only when evidence
  is strong. Otherwise it remains its own entity/candidate.
- Do not promote a candidate whose target is already the canonical target of a
  different source form (the `セツナ → 天津气刹那` failure).
- Entity merges are explicit/low-frequency: update `entity_id` on the donor's
  forms, preserve every form target, and log a version/event.
- Keep form insertion order for prompt-cache stability.

### Title patterns

Store title patterns in `term_forms` as `form_type='title_pattern'`:

- `source`: reserved key such as `@title:あなたの為のオーケストラ 其の{n}`.
- `target`: target template such as `为您独奏的管弦乐团 其{n}`.
- `metadata`/template fields: variable type, ordinal style, marker, suffix.

Normalization is deterministic:

1. Parse the source title into fixed prefix, ordinal, suffix and marker.
2. Look up the pattern; render the target from the canonical template and a
   single number formatter.
3. If no pattern exists, use the model title once, derive a provisional
   template, normalize the ordinal, and register it.
4. Apply the rendered title before storing the chapter; ignore model variation
   in the fixed part/ordinal style.
5. A title check verifies every matching chapter uses the registered template.
6. Pattern changes update `title_zh` only; no chapter body re-translation.

Only repeated/known patterns are normalized; unique titles remain untouched.

### Strict adjudication and manual takeover

Strict mode changes unresolved major findings from a hard pipeline block into a
manual-review gate:

- Add `adjudication_mode = "normal"|"strict"`.
- Add a `manual_review` chapter state. Unresolved major after the revision
  budget writes the chapter and event to the manual queue; it does not stop the
  translation scheduler.
- Add `ready` for automatically reviewed downstream batches that cannot be
  accepted until an earlier manual hold resolves.
- Add `stale` for downstream results invalidated by a manual text/term change.
- Acceptance/export remains a contiguous accepted-prefix gate; manual holds
  never become accepted or exported automatically.
- Translation gate remains index materialization, not review completion.
- `pipeline_manual_backlog` bounds pending manual holds; exceeding it pauses
  new translation but not in-flight work.
- Manual API: `manual_review_queue()` and
  `resolve_manual_review(order, decision="accept"|"revise"|"blocked",
  text=None, term_changes=None, notes="")`.
- A manual text/term change bumps a term/index version; downstream chapters
  using the old provisional context are marked stale and re-reviewed or
  re-translated. Accept-only/dismiss-only decisions can promote ready batches
  without rework.

### Round 2 implementation checklist

- [x] Plan document updated with the three designs.
- [x] `term_entities`/`term_forms` schema, migration, compatibility query,
  put/confirm/promote/link/merge operations.
- [x] Target-conflict promotion guard.
- [x] Title pattern parser/renderer, term-form integration, title check.
- [x] Strict adjudication mode, manual queue, ready states, manual accept/revise
  API; `mark_term_revisions` is the term-change primitive.
- [x] Tests for full/short forms, title patterns, manual hold + continued
  translation, ready acceptance, entity lookup.
- [x] README/docs/skill operations updated; global skill copies synced.
- [x] Full suite green: 145 passed.
- [ ] Automatic global term adjudication and automatic downstream stale/rework
  scheduling; manual `term_changes` currently records affected chapters but
  does not auto-requeue them.
