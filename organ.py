#!/usr/bin/env python3
"""
Persona Seed-Dedupe Organ — extracted decision logic from discovery-engine.

A pure decider that decides whether a persona's about-to-be-seeded work item is
a duplicate of an existing GitHub PR. Standing-directive personas re-evaluate the
same gap on every cron tick; without a PR-state check they happily re-seed a brief
after a fix has already shipped, and local-cron workers chew through "claimed"
items that all describe the same already-resolved problem.

The original service (``app/services/persona_seed_dedupe.py``) mixed two concerns:
  1. FETCH — query the GitHub Search API for candidate PRs.
  2. DECIDE — classify those candidates and choose the dedup verdict.

This organ is the pure DECIDE half. The spine (the caller) does the fetch and
hands the candidate PRs in via ``state``. The organ never touches the network.

Contract:
  INPUT state: {
    "mode": "keywords" | "issue",        # default "keywords"
    "issue_number": int | null,          # required for mode="issue"
    "candidates": [                       # pre-fetched PR candidates
      {
        "number": int,
        "title": str,
        "state": "open" | "closed",
        "merged": bool,                  # only meaningful when state=="closed"
        "url": str,
        "body": str,                     # used for issue-ref / recency checks
        "updated_at": str | null         # GitHub ISO-8601, e.g. 2026-06-03T10:11:12Z
      },
      ...
    ],
    "window_hours": int,                 # mode="issue" recency window, default 8
    "now": str | null                    # mode="issue" current time, ISO-8601 UTC
  }

  OUTPUT: {
    "output": {
      "verdict": "seed"                  # no overlap — go ahead and seed
                 | "skip_duplicate"      # a MERGED PR already closes the gap
                 | "downgrade_to_verification",  # an OPEN PR is mid-fix
      "match": {number,title,state,merged,url} | null
    },
    "rationale": "...",
    "self_metric": {
      "confidence": float,               # 1.0 when inputs sufficient, lower when not
      "candidates_evaluated": int,
      "candidates_considered": int,      # after issue-mode ref/recency filtering
      "decision_path": str
    }
  }

Verdict mapping mirrors the source's best-match preference
(merged > open > closed-not-merged):
  - a merged candidate  -> "skip_duplicate"            (gap already fixed)
  - an open candidate    -> "downgrade_to_verification" (someone is mid-fix)
  - none of the above    -> "seed"                      (gap is still real)

The organ is pure:
  - Takes all inputs via JSON; no DB/network calls.
  - Deterministic given the same input.
  - Fail-safe to the CONSERVATIVE verdict ("seed") on malformed/empty state —
    over-seeding is recoverable; wrongly skipping a real gap is not.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone


def _parse_gh_datetime(value):
    """Parse a GitHub ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _classify(candidate):
    """Return a normalised PrMatch-shaped dict from a raw candidate dict."""
    pr_state = candidate.get("state") or ""
    return {
        "number": candidate.get("number", 0),
        "title": candidate.get("title", ""),
        "state": pr_state,
        "merged": bool(candidate.get("merged")),
        "url": candidate.get("url", ""),
    }


def _pick_best(candidates):
    """Choose the best match: a merged PR wins outright; else the first open PR.

    Returns (match_dict_or_None, considered_count). Mirrors the source loop which
    returned immediately on the first merged PR and otherwise held the first open
    one.
    """
    best = None
    for cand in candidates:
        match = _classify(cand)
        if match["merged"]:
            return match, len(candidates)
        if match["state"] == "open" and best is None:
            best = match
    return best, len(candidates)


def _filter_issue_candidates(candidates, issue_number, cutoff):
    """Keep only candidates that genuinely reference ``#issue_number`` (title or
    body, word-boundary so #484 doesn't match #4840) AND were updated on/after
    ``cutoff`` (when an ``updated_at`` is present)."""
    ref = re.compile(rf"#{issue_number}\b")
    kept = []
    for cand in candidates:
        haystack = f"{cand.get('title') or ''}\n{cand.get('body') or ''}"
        if not ref.search(haystack):
            continue
        if cutoff is not None:
            updated = _parse_gh_datetime(cand.get("updated_at"))
            if updated is not None and updated < cutoff:
                continue
        kept.append(cand)
    return kept


def _verdict_for(match):
    """Map a best-match (or None) to the seed verdict."""
    if match is None:
        return "seed"
    if match["merged"]:
        return "skip_duplicate"
    if match["state"] == "open":
        return "downgrade_to_verification"
    # closed-but-not-merged: the gap was punted and is still real -> seed.
    return "seed"


def decide(state: dict, context: dict | None = None) -> dict:
    """Decide whether a persona seed is a duplicate of an existing PR.

    Args:
        state: see module docstring.
        context: unused; present for orchestrator compatibility.

    Returns:
        {"output": {verdict, match}, "rationale": "...", "self_metric": {...}}
    """
    context = context or {}

    try:
        if not isinstance(state, dict):
            raise ValueError("state must be an object")

        mode = state.get("mode") or "keywords"
        candidates = state.get("candidates")
        if candidates is None:
            candidates = []
        if not isinstance(candidates, list):
            raise ValueError("candidates must be a list")

        evaluated = len(candidates)

        if mode == "issue":
            issue_number = state.get("issue_number")
            if not isinstance(issue_number, int) or issue_number <= 0:
                # Can't anchor the dedup -> conservative: seed.
                return {
                    "output": {"verdict": "seed", "match": None},
                    "rationale": (
                        "issue mode requires a positive integer issue_number; "
                        "none provided — defaulting to seed (over-seed is safe)."
                    ),
                    "self_metric": {
                        "confidence": 0.3,
                        "candidates_evaluated": evaluated,
                        "candidates_considered": 0,
                        "decision_path": "issue_missing_number",
                    },
                }

            window_hours = state.get("window_hours", 8)
            try:
                window_hours = max(0, int(window_hours))
            except (TypeError, ValueError):
                window_hours = 8

            now = _parse_gh_datetime(state.get("now"))
            cutoff = None
            recency_known = now is not None
            if recency_known:
                cutoff = now - timedelta(hours=window_hours)

            considered = _filter_issue_candidates(candidates, issue_number, cutoff)
            match, _ = _pick_best(considered)
            verdict = _verdict_for(match)
            # Confidence drops when we couldn't apply the recency window because
            # `now` was absent — the result may include stale PRs.
            confidence = 1.0 if recency_known else 0.7
            if match is None:
                rationale = (
                    f"No PR in the last {window_hours}h references issue "
                    f"#{issue_number} (of {len(considered)} considered, "
                    f"{evaluated} evaluated) — seed the gap."
                )
            else:
                rationale = (
                    f"PR #{match['number']} ({'merged' if match['merged'] else match['state']}) "
                    f"references issue #{issue_number} within {window_hours}h -> {verdict}."
                )
            return {
                "output": {"verdict": verdict, "match": match},
                "rationale": rationale,
                "self_metric": {
                    "confidence": confidence,
                    "candidates_evaluated": evaluated,
                    "candidates_considered": len(considered),
                    "decision_path": "issue_recency",
                },
            }

        # keywords mode (default): trust the caller's keyword-filtered candidates
        # and classify them, exactly as the source did with GitHub's search hits.
        match, considered = _pick_best(candidates)
        verdict = _verdict_for(match)
        if match is None:
            rationale = (
                f"No open or merged PR among {evaluated} keyword candidate(s) "
                f"— seed the gap."
            )
        else:
            rationale = (
                f"PR #{match['number']} "
                f"({'merged' if match['merged'] else match['state']}) overlaps the "
                f"keywords -> {verdict}."
            )
        return {
            "output": {"verdict": verdict, "match": match},
            "rationale": rationale,
            "self_metric": {
                "confidence": 1.0,
                "candidates_evaluated": evaluated,
                "candidates_considered": considered,
                "decision_path": "keywords",
            },
        }

    except Exception as e:
        # Fail-safe to the conservative verdict: seed. Over-seeding is recoverable;
        # wrongly skipping a real gap silently drops work.
        return {
            "output": {"verdict": "seed", "match": None},
            "rationale": f"Decision logic error (fail-safe to seed): {e}",
            "self_metric": {
                "confidence": 0.0,
                "candidates_evaluated": 0,
                "candidates_considered": 0,
                "decision_path": "error_fallback",
            },
        }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
