"""
Pytest test suite for the persona seed-dedupe organ.

Covers the pure decision logic extracted from discovery-engine's
``app/services/persona_seed_dedupe.py``:
  - keyword-mode best-match preference (merged > open > closed-not-merged)
  - issue-mode reference matching (#484 word boundary) + recency window
  - verdict mapping (seed / skip_duplicate / downgrade_to_verification)
  - fail-safe-to-seed behaviour on malformed / insufficient state
  - the orchestrator output envelope shape
"""

import copy

import pytest

from organ import decide


def _merged(number=100, title="Fix signup lead capture"):
    return {
        "number": number,
        "title": title,
        "state": "closed",
        "merged": True,
        "url": f"https://github.com/o/r/pull/{number}",
        "body": f"Closes #484\n{title}",
        "updated_at": "2026-06-03T10:00:00Z",
    }


def _open(number=101, title="WIP signup lead capture"):
    return {
        "number": number,
        "title": title,
        "state": "open",
        "merged": False,
        "url": f"https://github.com/o/r/pull/{number}",
        "body": f"Refs #484\n{title}",
        "updated_at": "2026-06-03T10:00:00Z",
    }


def _closed_unmerged(number=102, title="Punted signup work"):
    return {
        "number": number,
        "title": title,
        "state": "closed",
        "merged": False,
        "url": f"https://github.com/o/r/pull/{number}",
        "body": f"References #484\n{title}",
        "updated_at": "2026-06-03T10:00:00Z",
    }


class TestEnvelopeShape:
    def test_output_envelope_keys(self):
        result = decide({"mode": "keywords", "candidates": []})
        assert set(result.keys()) == {"output", "rationale", "self_metric"}
        assert "verdict" in result["output"]
        assert "match" in result["output"]
        assert "confidence" in result["self_metric"]
        assert isinstance(result["rationale"], str)

    def test_confidence_in_range(self):
        for state in (
            {"candidates": [_merged()]},
            {"mode": "issue", "issue_number": 484, "candidates": []},
            {"mode": "issue", "candidates": []},
        ):
            c = decide(state)["self_metric"]["confidence"]
            assert 0.0 <= c <= 1.0


class TestKeywordMode:
    def test_empty_candidates_seeds(self):
        result = decide({"mode": "keywords", "candidates": []})
        assert result["output"]["verdict"] == "seed"
        assert result["output"]["match"] is None
        assert result["self_metric"]["confidence"] == 1.0

    def test_default_mode_is_keywords(self):
        result = decide({"candidates": []})
        assert result["self_metric"]["decision_path"] == "keywords"

    def test_merged_pr_skips_duplicate(self):
        result = decide({"candidates": [_merged()]})
        assert result["output"]["verdict"] == "skip_duplicate"
        assert result["output"]["match"]["number"] == 100
        assert result["output"]["match"]["merged"] is True

    def test_open_pr_downgrades(self):
        result = decide({"candidates": [_open()]})
        assert result["output"]["verdict"] == "downgrade_to_verification"
        assert result["output"]["match"]["state"] == "open"

    def test_closed_unmerged_still_seeds(self):
        result = decide({"candidates": [_closed_unmerged()]})
        assert result["output"]["verdict"] == "seed"
        assert result["output"]["match"] is None

    def test_merged_preferred_over_open(self):
        # Even with an open PR listed first, a merged PR wins.
        result = decide({"candidates": [_open(), _merged()]})
        assert result["output"]["verdict"] == "skip_duplicate"
        assert result["output"]["match"]["merged"] is True

    def test_first_open_held_when_no_merged(self):
        result = decide({"candidates": [_open(101), _open(202)]})
        assert result["output"]["match"]["number"] == 101

    def test_candidates_evaluated_counted(self):
        result = decide({"candidates": [_open(), _closed_unmerged()]})
        assert result["self_metric"]["candidates_evaluated"] == 2


class TestIssueMode:
    def test_recent_merged_pr_skips(self):
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [_merged()],
            "now": "2026-06-03T12:00:00Z",
            "window_hours": 8,
        }
        result = decide(state)
        assert result["output"]["verdict"] == "skip_duplicate"
        assert result["self_metric"]["decision_path"] == "issue_recency"
        assert result["self_metric"]["confidence"] == 1.0

    def test_recent_open_pr_downgrades(self):
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [_open()],
            "now": "2026-06-03T12:00:00Z",
        }
        result = decide(state)
        assert result["output"]["verdict"] == "downgrade_to_verification"

    def test_stale_pr_outside_window_seeds(self):
        # PR updated 20h before 'now' with an 8h window -> filtered out -> seed.
        cand = _merged()
        cand["updated_at"] = "2026-06-02T16:00:00Z"
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [cand],
            "now": "2026-06-03T12:00:00Z",
            "window_hours": 8,
        }
        result = decide(state)
        assert result["output"]["verdict"] == "seed"
        assert result["self_metric"]["candidates_considered"] == 0

    def test_word_boundary_rejects_superstring(self):
        # A PR referencing #4840 must NOT match issue #484.
        cand = _merged()
        cand["title"] = "Fix #4840 unrelated"
        cand["body"] = "Closes #4840"
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [cand],
            "now": "2026-06-03T12:00:00Z",
        }
        result = decide(state)
        assert result["output"]["verdict"] == "seed"

    def test_non_referencing_pr_filtered(self):
        cand = _merged()
        cand["title"] = "Totally unrelated work"
        cand["body"] = "no issue ref here"
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [cand],
            "now": "2026-06-03T12:00:00Z",
        }
        result = decide(state)
        assert result["output"]["verdict"] == "seed"
        assert result["self_metric"]["candidates_considered"] == 0

    def test_missing_now_lowers_confidence_but_still_matches(self):
        # Without `now` the recency window can't apply; still classify, lower conf.
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [_merged()],
        }
        result = decide(state)
        assert result["output"]["verdict"] == "skip_duplicate"
        assert result["self_metric"]["confidence"] == 0.7

    def test_missing_issue_number_seeds(self):
        state = {"mode": "issue", "candidates": [_merged()]}
        result = decide(state)
        assert result["output"]["verdict"] == "seed"
        assert result["self_metric"]["decision_path"] == "issue_missing_number"
        assert result["self_metric"]["confidence"] == 0.3

    def test_invalid_issue_number_seeds(self):
        state = {"mode": "issue", "issue_number": -5, "candidates": [_merged()]}
        result = decide(state)
        assert result["output"]["verdict"] == "seed"

    def test_window_hours_default_is_eight(self):
        # 9h old with default window -> filtered out.
        cand = _open()
        cand["updated_at"] = "2026-06-03T03:00:00Z"
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [cand],
            "now": "2026-06-03T12:00:00Z",
        }
        result = decide(state)
        assert result["output"]["verdict"] == "seed"

    def test_merged_preferred_in_issue_mode(self):
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [_open(), _merged()],
            "now": "2026-06-03T12:00:00Z",
        }
        result = decide(state)
        assert result["output"]["match"]["merged"] is True


class TestFailSafe:
    def test_non_dict_state_seeds(self):
        result = decide("not a dict")  # type: ignore[arg-type]
        assert result["output"]["verdict"] == "seed"
        assert result["self_metric"]["decision_path"] == "error_fallback"
        assert result["self_metric"]["confidence"] == 0.0

    def test_candidates_not_a_list_seeds(self):
        result = decide({"candidates": {"oops": True}})
        assert result["output"]["verdict"] == "seed"
        assert result["self_metric"]["decision_path"] == "error_fallback"

    def test_none_candidates_treated_as_empty(self):
        result = decide({"candidates": None})
        assert result["output"]["verdict"] == "seed"
        assert result["self_metric"]["confidence"] == 1.0

    def test_bad_window_hours_falls_back_to_default(self):
        cand = _open()
        cand["updated_at"] = "2026-06-03T11:00:00Z"
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [cand],
            "now": "2026-06-03T12:00:00Z",
            "window_hours": "garbage",
        }
        result = decide(state)
        # 1h old, default 8h window -> within window -> downgrade.
        assert result["output"]["verdict"] == "downgrade_to_verification"

    def test_determinism(self):
        state = {
            "mode": "issue",
            "issue_number": 484,
            "candidates": [_open(), _merged(), _closed_unmerged()],
            "now": "2026-06-03T12:00:00Z",
        }
        a = decide(copy.deepcopy(state))
        b = decide(copy.deepcopy(state))
        assert a == b

    def test_no_side_effects_on_input(self):
        state = {"candidates": [_merged()]}
        snapshot = copy.deepcopy(state)
        decide(state)
        assert state == snapshot


class TestSamples:
    """Every committed sample parses and decides without error."""

    def test_samples_decide(self):
        import glob
        import json
        import os

        here = os.path.dirname(__file__)
        samples = glob.glob(os.path.join(here, "samples", "*.json"))
        assert samples, "no samples found"
        for path in samples:
            with open(path) as fh:
                payload = json.load(fh)
            result = decide(payload["state"], payload.get("context"))
            assert result["output"]["verdict"] in (
                "seed",
                "skip_duplicate",
                "downgrade_to_verification",
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
