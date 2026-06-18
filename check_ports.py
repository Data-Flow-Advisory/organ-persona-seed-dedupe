#!/usr/bin/env python3
"""Port-declaration conformance check (the connection-standard's port check).

Asserts, per CONNECTORS.md:
  1. ``ports.json`` parses and has the right shape
     ({"inputs": [{name,type,required}], "outputs": [{name,type}]}).
  2. Every ``type`` referenced by a port exists in the shared vocabulary
     (``types.json`` — vendored here from the canonical
     ``Data-Flow-Advisory/orchestrator@feat/drift-gate/types.json`` plus this
     organ's proposed additions).
  3. ``decide`` actually READS each declared input name under ``state`` — proven
     at runtime by shadow-running the organ on its own samples through a
     read-tracking ``state`` proxy (records every ``[]`` / ``.get`` / ``in``).
  4. ``decide`` actually WRITES each declared output name under ``output`` —
     proven by inspecting the shadow-run ``output`` dicts.

Read/write evidence is COLLECTIVE: a declared name must be exercised by at least
one committed sample (no single sample need hit every branch). Exit non-zero on
any failure so the conformance Action goes red.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


class _TrackingState(dict):
    """A ``state`` dict that records every key the organ actually touches.

    ``dict.get`` / ``__getitem__`` / ``__contains__`` are all overridden because
    the organ reaches keys via a mix of ``state.get("k")``, ``state["k"]`` and
    ``"k" in state``; ``dict.get`` is a C-level method, so it MUST be overridden
    explicitly or the access goes unrecorded.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.touched: set = set()

    def get(self, key, default=None):
        self.touched.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.touched.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.touched.add(key)
        return super().__contains__(key)


def _load_json(name: str):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        _fail(f"{name} is missing")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        _fail(f"{name} does not parse as JSON: {e}")


def _validate_shape(ports: dict) -> None:
    if not isinstance(ports, dict):
        _fail("ports.json must be a JSON object")
    for side in ("inputs", "outputs"):
        if side not in ports:
            _fail(f"ports.json missing '{side}'")
        if not isinstance(ports[side], list):
            _fail(f"ports.json '{side}' must be a list")
    for p in ports["inputs"]:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"input port malformed (need name+type): {p!r}")
        if "required" in p and not isinstance(p["required"], bool):
            _fail(f"input port 'required' must be a bool: {p!r}")
    for p in ports["outputs"]:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"output port malformed (need name+type): {p!r}")


def _validate_types(ports: dict, vocab: dict) -> None:
    types = vocab.get("types", {})
    if not isinstance(types, dict) or not types:
        _fail("types.json has no 'types' object")
    for p in ports["inputs"] + ports["outputs"]:
        if p["type"] not in types:
            _fail(
                f"port {p['name']!r} references type {p['type']!r} "
                f"which is not in the type vocabulary (types.json)"
            )


def _read_write_evidence(ports: dict) -> None:
    import organ  # imported here so a syntax error surfaces as a check failure

    sample_dir = os.path.join(HERE, "samples")
    samples = []
    if os.path.isdir(sample_dir):
        for fn in sorted(os.listdir(sample_dir)):
            if fn.endswith(".json"):
                with open(os.path.join(sample_dir, fn)) as f:
                    samples.append((fn, json.load(f)))
    if not samples:
        _fail("no samples/ to witness reads+writes against")

    read_seen: set = set()
    write_seen: set = set()
    for fn, payload in samples:
        tracking = _TrackingState(payload.get("state", {}))
        result = organ.decide(tracking, payload.get("context"))
        read_seen |= tracking.touched
        out = result.get("output") if isinstance(result, dict) else None
        if isinstance(out, dict):
            write_seen |= set(out.keys())

    for p in ports["inputs"]:
        if p["name"] not in read_seen:
            _fail(
                f"declared input {p['name']!r} is never read from state by "
                f"decide() across any committed sample"
            )
    for p in ports["outputs"]:
        if p["name"] not in write_seen:
            _fail(
                f"declared output {p['name']!r} is never written under output by "
                f"decide() across any committed sample"
            )


def main() -> int:
    ports = _load_json("ports.json")
    vocab = _load_json("types.json")
    _validate_shape(ports)
    _validate_types(ports, vocab)
    _read_write_evidence(ports)
    in_names = [p["name"] for p in ports["inputs"]]
    out_names = [p["name"] for p in ports["outputs"]]
    print(
        "OK: ports.json conforms — "
        f"inputs={in_names} outputs={out_names}; all types in vocabulary; "
        "every declared name read/written by decide() on samples."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
