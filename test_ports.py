"""
Tests for the connection-standard port declaration (ports.json) and its
checker (check_ports.py).

Positive: the committed ports.json matches organ.py and types.json.
Negative: check_ports.py actually goes RED when the declaration drifts
from the implementation (so the conformance gate is meaningful, not a
no-op). The negative cases tamper with ports.json / types.json in a temp
copy of the repo and assert a non-zero exit + a clear message.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _run_check(cwd):
    """Run check_ports.py in ``cwd`` and return (rc, combined_output)."""
    proc = subprocess.run(
        [sys.executable, "check_ports.py"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _staged_repo(tmp_path):
    """Copy the organ's port-relevant files into an isolated dir."""
    for fn in ("check_ports.py", "ports.json", "types.json", "organ.py"):
        shutil.copy(os.path.join(_HERE, fn), tmp_path / fn)
    return tmp_path


# --------------------------------------------------------------------------
# Positive: the real, committed declaration passes.
# --------------------------------------------------------------------------


def test_ports_json_is_wellformed():
    with open(os.path.join(_HERE, "ports.json")) as fh:
        ports = json.load(fh)
    assert isinstance(ports["inputs"], list)
    assert isinstance(ports["outputs"], list)
    for p in ports["inputs"]:
        assert isinstance(p["name"], str) and p["name"]
        assert isinstance(p["type"], str) and p["type"]
        assert isinstance(p["required"], bool)
    for p in ports["outputs"]:
        assert isinstance(p["name"], str) and p["name"]
        assert isinstance(p["type"], str) and p["type"]


def test_declared_names_match_organ():
    rc, out = _run_check(_HERE)
    assert rc == 0, out
    assert "ports check OK" in out


def test_declared_inputs_outputs_exact():
    with open(os.path.join(_HERE, "ports.json")) as fh:
        ports = json.load(fh)
    inputs = {p["name"] for p in ports["inputs"]}
    outputs = {p["name"] for p in ports["outputs"]}
    assert inputs == {"mode", "candidates", "issue_number", "window_hours", "now"}
    assert outputs == {"verdict", "match"}


def test_all_types_in_vocabulary():
    with open(os.path.join(_HERE, "ports.json")) as fh:
        ports = json.load(fh)
    with open(os.path.join(_HERE, "types.json")) as fh:
        vocab = set(json.load(fh)["types"].keys())
    for p in ports["inputs"] + ports["outputs"]:
        assert p["type"] in vocab, f"{p['name']} -> {p['type']}"


# --------------------------------------------------------------------------
# Negative: the checker must fail when the declaration drifts.
# --------------------------------------------------------------------------


def test_fails_when_input_declared_but_not_read(tmp_path):
    repo = _staged_repo(tmp_path)
    ports = json.loads((repo / "ports.json").read_text())
    ports["inputs"].append({"name": "ghost_input", "type": "string", "required": False})
    (repo / "ports.json").write_text(json.dumps(ports))
    rc, out = _run_check(repo)
    assert rc != 0
    assert "declared inputs do not match" in out


def test_fails_when_output_declared_but_not_written(tmp_path):
    repo = _staged_repo(tmp_path)
    ports = json.loads((repo / "ports.json").read_text())
    ports["outputs"].append({"name": "ghost_output", "type": "object"})
    (repo / "ports.json").write_text(json.dumps(ports))
    rc, out = _run_check(repo)
    assert rc != 0
    assert "declared outputs do not match" in out


def test_fails_when_input_dropped(tmp_path):
    repo = _staged_repo(tmp_path)
    ports = json.loads((repo / "ports.json").read_text())
    ports["inputs"] = [p for p in ports["inputs"] if p["name"] != "now"]
    (repo / "ports.json").write_text(json.dumps(ports))
    rc, out = _run_check(repo)
    assert rc != 0
    assert "declared inputs do not match" in out


def test_fails_when_type_not_in_vocabulary(tmp_path):
    repo = _staged_repo(tmp_path)
    ports = json.loads((repo / "ports.json").read_text())
    ports["inputs"][0]["type"] = "not_a_real_type"
    (repo / "ports.json").write_text(json.dumps(ports))
    rc, out = _run_check(repo)
    assert rc != 0
    assert "not in types.json" in out


def test_fails_when_ports_json_malformed(tmp_path):
    repo = _staged_repo(tmp_path)
    (repo / "ports.json").write_text("{ this is not json }")
    rc, out = _run_check(repo)
    assert rc != 0
    assert "does not parse" in out


def test_fails_when_required_not_boolean(tmp_path):
    repo = _staged_repo(tmp_path)
    ports = json.loads((repo / "ports.json").read_text())
    ports["inputs"][0]["required"] = "yes"
    (repo / "ports.json").write_text(json.dumps(ports))
    rc, out = _run_check(repo)
    assert rc != 0
    assert "boolean 'required'" in out
