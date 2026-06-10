# Persona Seed-Dedupe Organ

A pure decider that decides whether a persona's about-to-be-seeded work item is a
duplicate of an existing GitHub PR. Extracted from discovery-engine's
[`app/services/persona_seed_dedupe.py`](https://github.com/jdogweb/discovery-engine/blob/main/app/services/persona_seed_dedupe.py).

## Why it exists

Standing-directive personas (Tim, Sam, Riley, the CTO issue-addressing run, …)
re-evaluate the same gap on every cron tick. Without a PR-state check they happily
re-seed a brief after a fix has already shipped, and local-cron workers then chew
through "claimed" items that all describe the same already-resolved problem (the
Tim signup-attempts respawn pattern, and the issue #484 cross-repo double-seed
race that shipped two PRs minutes apart).

The original service mixed **fetch** (GitHub Search API) with **decide** (classify
the hits, choose a verdict). This organ is the pure **decide** half — the spine
does the fetch and hands the candidate PRs in via `state`. The organ never touches
the network, so the judgment layer can run it on the same input and compare.

## What it decides

Given a list of pre-fetched candidate PRs, it picks the best match
(merged > open > closed-not-merged) and maps it to a verdict:

| Best match              | Verdict                       | Meaning                                  |
| ----------------------- | ----------------------------- | ---------------------------------------- |
| a **merged** PR         | `skip_duplicate`              | the gap is already fixed — don't seed    |
| an **open** PR          | `downgrade_to_verification`   | someone is mid-fix — seed a check, not a dup |
| none / closed-unmerged  | `seed`                        | the gap is still real — go ahead         |

Two modes:

- **`keywords`** (default) — trust the caller's keyword-filtered candidates and
  classify them. Mirrors the source `seed_is_obsolete_due_to_pr`.
- **`issue`** — additionally filter candidates to those that reference
  `#<issue_number>` (word-boundary, so `#484` ≠ `#4840`) and were updated within
  `window_hours` of `now`. Mirrors the source `issue_recently_addressed`.

## Input contract

```json
{
  "state": {
    "mode": "keywords",
    "issue_number": 484,
    "window_hours": 8,
    "now": "2026-06-03T12:00:00Z",
    "candidates": [
      {
        "number": 884,
        "title": "Fix signup lead capture",
        "state": "closed",
        "merged": true,
        "url": "https://github.com/jdogweb/discovery-engine/pull/884",
        "body": "Closes #484",
        "updated_at": "2026-06-03T10:00:00Z"
      }
    ]
  }
}
```

### Fields

- **mode** (str) — `"keywords"` (default) or `"issue"`.
- **candidates** (list) — pre-fetched PR dicts. Each: `number`, `title`, `state`
  (`"open"`/`"closed"`), `merged` (bool), `url`, `body`, `updated_at` (GitHub ISO).
- **issue_number** (int) — required for `mode="issue"`; a positive integer.
- **window_hours** (int) — `mode="issue"` recency window; default `8`.
- **now** (str) — `mode="issue"` current time, ISO-8601 UTC. When absent the
  recency filter is skipped and `self_metric.confidence` drops to `0.7`.

## Output contract

```json
{
  "output": {
    "verdict": "skip_duplicate",
    "match": {
      "number": 884,
      "title": "Fix signup lead capture",
      "state": "closed",
      "merged": true,
      "url": "https://github.com/jdogweb/discovery-engine/pull/884"
    }
  },
  "rationale": "PR #884 (merged) overlaps the keywords -> skip_duplicate.",
  "self_metric": {
    "confidence": 1.0,
    "candidates_evaluated": 1,
    "candidates_considered": 1,
    "decision_path": "keywords"
  }
}
```

`match` is `null` when the verdict is `seed`.

## Purity & fail-safe

- **No side effects** — no network, no DB, no mutation of the input.
- **Deterministic** given the same input.
- **Fail-safe to the conservative verdict** — on malformed / insufficient state the
  organ returns `seed` (over-seeding is recoverable; wrongly skipping a real gap
  silently drops work). See `decision_path: "error_fallback"`.

## Run it

```bash
# stdin
echo '{"state":{"candidates":[]}}' | python3 organ.py

# or via a sample file
ORGAN_INPUT=samples/keyword_merged_skip.json python3 organ.py

# tests
python -m pytest -v
```

## Contract

This organ conforms to the orchestrator organ contract: a pure decider, facts in →
advice out, no side effects. See the orchestrator's `CONTRACT.md`.
