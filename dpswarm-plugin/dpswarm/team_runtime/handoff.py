"""Finite, source-bound handoff validation; this module never reads task files.

Passing this contract proves the enumerated facts were relayed, not that every
natural-language requirement has been understood or that the task is complete.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from typing import Any


def _check_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _check_json(item)
        return
    raise ValueError("Contract facts must contain finite JSON values only")


def _schema(value: Any) -> dict[str, Any]:
    if type(value) is dict:
        return {"type": "object", "properties": {k: _schema(v) for k, v in value.items()},
                "required": list(value), "additionalProperties": False}
    if type(value) is list:
        # Ordered lists are part of this finite contract. Their exact contents
        # remain in the public spec, rather than being supplied as schema consts.
        if not value:
            return {"type": "array", "items": {}, "maxItems": 0}
        item_schemas = [_schema(v) for v in value]
        unique = {json.dumps(s, sort_keys=True): s for s in item_schemas}
        items = next(iter(unique.values())) if len(unique) == 1 else {"anyOf": list(unique.values())}
        return {"type": "array", "items": items, "minItems": len(value), "maxItems": len(value)}
    kind = {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}[type(value)]
    return {"type": kind}


def _compare(expected: Any, actual: Any, path: str, errors: list[str], missing: list[str]) -> None:
    if type(actual) is not type(expected):
        errors.append(f"{path}: incorrect JSON type")
        return
    if type(expected) is dict:
        for key in expected:
            child = f"{path}.{key}"
            if key not in actual:
                missing.append(child)
            else:
                _compare(expected[key], actual[key], child, errors, missing)
        for key in actual:
            if key not in expected:
                errors.append(f"{path}.{key}: unknown field")
    elif type(expected) is list:
        if len(actual) != len(expected):
            errors.append(f"{path}: incorrect ordered-list length")
        for index, (want, got) in enumerate(zip(expected, actual)):
            _compare(want, got, f"{path}[{index}]", errors, missing)
    elif actual != expected:
        # Feedback identifies the mismatch, without supplying the answer.
        errors.append(f"{path}: value does not match the authorized public spec")


class HandoffContract:
    """An immutable-by-interface collection of independently verifiable facts."""

    _FIELDS = ("task_id", "source_spec_sha256", "source_ref", "facts", "assumptions", "unresolved")

    def __init__(self, task_id: str, source_spec_sha256: str,
                 required_facts: dict[str, Any], deliverables: list[str],
                 source_refs: dict[str, dict[str, Any]]):
        if type(task_id) is not str or not task_id:
            raise ValueError("task_id must be a nonempty string")
        if type(source_spec_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", source_spec_sha256):
            raise ValueError("source_spec_sha256 must be a lowercase SHA-256 digest")
        if type(required_facts) is not dict or not required_facts:
            raise ValueError("required_facts must be a nonempty object")
        if type(deliverables) is not list or not deliverables or any(type(v) is not str or not v for v in deliverables):
            raise ValueError("deliverables must be a nonempty list of paths")
        facts = deepcopy(required_facts)
        if "deliverables" in facts and facts["deliverables"] != deliverables:
            raise ValueError("Conflicting deliverables in contract")
        facts["deliverables"] = deepcopy(deliverables)
        _check_json(facts)
        if type(source_refs) is not dict or not source_refs:
            raise ValueError("Public source references are required")
        for path, ref in source_refs.items():
            if (type(path) is not str or not path.startswith("facts.") or type(ref) is not dict
                    or set(ref) != {"source_ref", "start_line", "end_line"}
                    or ref["source_ref"] != "spec" or type(ref["start_line"]) is not int
                    or type(ref["end_line"]) is not int
                    or not 1 <= ref["start_line"] <= ref["end_line"]):
                raise ValueError("Source references must identify line ranges in public spec")
        self._task_id = task_id
        self._source_spec_sha256 = source_spec_sha256
        self._facts = facts
        self._deliverables = deepcopy(deliverables)
        self._source_refs = deepcopy(source_refs)

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def source_spec_sha256(self) -> str:
        return self._source_spec_sha256

    @property
    def required_facts(self) -> dict[str, Any]:
        return deepcopy(self._facts)

    @property
    def deliverables(self) -> list[str]:
        return deepcopy(self._deliverables)

    @property
    def source_refs(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._source_refs)

    def tool_declaration(self) -> dict[str, Any]:
        parameters = {
            "type": "object", "additionalProperties": False, "required": list(self._FIELDS),
            "properties": {
                "task_id": {"type": "string"}, "source_spec_sha256": {"type": "string"},
                "source_ref": {"type": "string", "enum": ["spec"]},
                "facts": _schema(self._facts),
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "unresolved": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
            },
        }
        return {"type": "function", "function": {"name": "submit_handoff",
                "description": "Submit the complete finite facts from the authorized public specification. A message or prose plan is not an accepted handoff.",
                "parameters": parameters}}

    def prompt_instructions(self) -> str:
        refs = json.dumps(self._source_refs, ensure_ascii=False, sort_keys=True)
        return (
            f"Submit task_id={self.task_id!r}, source_ref='spec', "
            f"source_spec_sha256={self.source_spec_sha256!r} with submit_handoff.\n"
            "The Executor cannot read the full spec. Relay every required fact in the tool schema, "
            "including exact key-bound values, applicable constraints, and deliverable paths. "
            "Use JSON booleans/numbers with the specified types, not strings; preserve source list order. "
            "Never substitute 'use the exact value' for that value or infer missing mappings from a prefix. "
            "For priority ranks use the API source identifiers and 1 for highest priority. "
            "Unresolved requirements must be resolved from your authorized spec before acceptance. "
            "Keep optional, non-normative suggestions in assumptions; they cannot override facts. "
            "A successful send_message is not acceptance. If validation reports missing or wrong fields, "
            "reread the referenced public source and resubmit within your phase budget. "
            "Do not read or cite grader, expected answers, prior scores, or other runs. "
            "This finite contract does not certify all natural-language semantics or task completion.\n"
            f"Authorized public source locations: {refs}"
        )

    def validate(self, payload: Any) -> dict[str, Any]:
        errors: list[str] = []
        missing: list[str] = []
        if type(payload) is not dict:
            return {"ok": False, "errors": ["payload: expected a structured object"], "missing": list(self._FIELDS)}
        for key in self._FIELDS:
            if key not in payload:
                missing.append(key)
        for key in payload:
            if key not in self._FIELDS:
                errors.append(f"{key}: unknown field")
        expected = {"task_id": self.task_id, "source_spec_sha256": self.source_spec_sha256,
                    "source_ref": "spec", "facts": self._facts}
        for key, value in expected.items():
            if key in payload:
                _compare(value, payload[key], key, errors, missing)
        for key in ("assumptions", "unresolved"):
            if key in payload:
                value = payload[key]
                if type(value) is not list or any(type(v) is not str for v in value):
                    errors.append(f"{key}: expected a list of strings")
                elif key == "unresolved" and value:
                    errors.append("unresolved: unresolved requirements prevent acceptance")
        return {"ok": not errors and not missing, "errors": errors, "missing": missing}

    def accepted_package(self, payload: Any) -> dict[str, Any]:
        verdict = self.validate(payload)
        if not verdict["ok"]:
            raise ValueError("Handoff rejected: " + json.dumps(verdict, ensure_ascii=False))
        result = deepcopy(payload)
        result["handoff_validated"] = True
        result["contract_version"] = 1
        result["source_refs"] = self.source_refs
        return result
