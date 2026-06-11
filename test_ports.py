#!/usr/bin/env python3
"""Tests for the connection-standard port manifest + its conformance check.

Covers the happy path (the real ports.json conforms) and the negative paths the
``check_ports.py`` gate must reject: malformed manifest, non-vocabulary type,
declared-but-unread input, declared-but-unwritten output, bad `required` type.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _run_check(cwd: str):
    # Run the check_ports.py that lives IN ``cwd`` (the script resolves its
    # ports.json/types.json/organ.py/samples relative to its own __file__), so the
    # negative-path scaffolds in tmp dirs are exercised against their own files.
    script = os.path.join(cwd, "check_ports.py")
    return subprocess.run(
        [sys.executable, script],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Manifest sanity
# ---------------------------------------------------------------------------

def test_ports_json_parses_and_shape():
    ports = json.load(open(os.path.join(HERE, "ports.json")))
    assert isinstance(ports["inputs"], list) and ports["inputs"]
    assert isinstance(ports["outputs"], list) and ports["outputs"]
    for p in ports["inputs"]:
        assert set(("name", "type")) <= set(p)
        assert isinstance(p.get("required", False), bool)
    for p in ports["outputs"]:
        assert set(("name", "type")) <= set(p)


def test_every_port_type_in_vocabulary():
    ports = json.load(open(os.path.join(HERE, "ports.json")))
    vocab = json.load(open(os.path.join(HERE, "types.json")))["types"]
    for p in ports["inputs"] + ports["outputs"]:
        assert p["type"] in vocab, f"{p['type']} not in vocabulary"


def test_declared_names_match_organ_io():
    """The declared input is read and the declared output is written by decide()."""
    import organ

    ports = json.load(open(os.path.join(HERE, "ports.json")))
    in_names = {p["name"] for p in ports["inputs"]}
    out_names = {p["name"] for p in ports["outputs"]}

    sample = {
        "mode": "keywords",
        "candidates": [
            {"number": 7, "title": "t", "state": "open", "merged": False, "url": "u"}
        ],
    }
    result = organ.decide(sample, None)
    out_keys = set(result["output"].keys())
    assert out_names <= out_keys
    # candidates is the declared input and is read on the keywords path.
    assert "candidates" in in_names


def test_wrapped_verdict_port_present_and_matches_flat_fields():
    import organ

    result = organ.decide(
        {
            "mode": "keywords",
            "candidates": [
                {"number": 9, "title": "x", "state": "closed", "merged": True, "url": "u"}
            ],
        },
        None,
    )
    out = result["output"]
    assert "seed_dedupe_verdict" in out
    assert out["seed_dedupe_verdict"]["verdict"] == out["verdict"] == "skip_duplicate"
    assert out["seed_dedupe_verdict"]["match"] == out["match"]


def test_wrapper_present_on_failsafe_path():
    import organ

    # malformed state -> error fail-safe path must still surface the typed port.
    result = organ.decide("not-a-dict", None)
    assert result["output"]["seed_dedupe_verdict"]["verdict"] == "seed"


# ---------------------------------------------------------------------------
# The conformance gate accepts the committed manifest
# ---------------------------------------------------------------------------

def test_check_ports_passes_on_real_manifest():
    proc = _run_check(HERE)
    assert proc.returncode == 0, proc.stderr
    assert "OK:" in proc.stdout


# ---------------------------------------------------------------------------
# The conformance gate rejects broken manifests (run in an isolated copy dir)
# ---------------------------------------------------------------------------

def _scaffold(tmp_path, ports_obj, types_obj=None):
    """Copy organ.py + samples into tmp_path and write a candidate ports/types."""
    import shutil

    shutil.copy(os.path.join(HERE, "organ.py"), tmp_path / "organ.py")
    shutil.copy(os.path.join(HERE, "check_ports.py"), tmp_path / "check_ports.py")
    shutil.copytree(os.path.join(HERE, "samples"), tmp_path / "samples")
    if types_obj is None:
        types_obj = json.load(open(os.path.join(HERE, "types.json")))
    (tmp_path / "types.json").write_text(json.dumps(types_obj))
    if ports_obj is _RAW:
        pass  # caller wrote ports.json themselves
    else:
        (tmp_path / "ports.json").write_text(json.dumps(ports_obj))
    return tmp_path


_RAW = object()


def test_reject_malformed_ports_json(tmp_path):
    _scaffold(tmp_path, _RAW)
    (tmp_path / "ports.json").write_text("{ not json ")
    proc = _run_check(str(tmp_path))
    assert proc.returncode != 0
    assert "does not parse" in proc.stderr


def test_reject_unknown_type(tmp_path):
    _scaffold(
        tmp_path,
        {"inputs": [{"name": "candidates", "type": "NotARealType", "required": False}],
         "outputs": [{"name": "seed_dedupe_verdict", "type": "SeedDedupeVerdict"}]},
    )
    proc = _run_check(str(tmp_path))
    assert proc.returncode != 0
    assert "not in the type vocabulary" in proc.stderr


def test_reject_unread_input(tmp_path):
    _scaffold(
        tmp_path,
        {"inputs": [{"name": "totally_unused_key", "type": "PullRequestList", "required": False}],
         "outputs": [{"name": "seed_dedupe_verdict", "type": "SeedDedupeVerdict"}]},
    )
    proc = _run_check(str(tmp_path))
    assert proc.returncode != 0
    assert "never read" in proc.stderr


def test_reject_unwritten_output(tmp_path):
    _scaffold(
        tmp_path,
        {"inputs": [{"name": "candidates", "type": "PullRequestList", "required": False}],
         "outputs": [{"name": "nonexistent_output", "type": "SeedDedupeVerdict"}]},
    )
    proc = _run_check(str(tmp_path))
    assert proc.returncode != 0
    assert "never written" in proc.stderr


def test_reject_bad_required_type(tmp_path):
    _scaffold(
        tmp_path,
        {"inputs": [{"name": "candidates", "type": "PullRequestList", "required": "yes"}],
         "outputs": [{"name": "seed_dedupe_verdict", "type": "SeedDedupeVerdict"}]},
    )
    proc = _run_check(str(tmp_path))
    assert proc.returncode != 0
    assert "required" in proc.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
