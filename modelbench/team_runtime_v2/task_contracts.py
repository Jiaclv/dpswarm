"""Finite extractors for two known PUBLIC TeamBench specification formats.

No filesystem, grader, expected.json, generator, or model access is performed.
Missing or conflicting required patterns reject. Additional prose is not a
claim of semantic coverage; these extractors certify only the listed facts.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from dpswarm.team_runtime.handoff import HandoffContract


class _Spec:
    def __init__(self, text: str):
        self.text = text
        self.lines = text.splitlines()

    def find(self, pattern: str) -> tuple[int, re.Match]:
        matches = [(i, m) for i, line in enumerate(self.lines, 1) if (m := re.fullmatch(pattern, line))]
        if len(matches) != 1:
            raise ValueError(f"Unsupported public spec: expected one matching line for {pattern!r}")
        return matches[0]

    def contains(self, literal: str) -> int:
        for i, line in enumerate(self.lines, 1):
            if literal in line:
                return i
        raise ValueError(f"Unsupported public spec: missing {literal!r}")

    def section(self, title: str) -> tuple[int, int]:
        start = self.contains(title)
        end = next((i for i in range(start, len(self.lines)) if self.lines[i].startswith("## ")), len(self.lines))
        return start, end


def _ref(start: int, end: int | None = None) -> dict[str, Any]:
    return {"source_ref": "spec", "start_line": start, "end_line": end or start}


def _path(value: str) -> str:
    if not value or value.startswith(("/", "\\")) or "\\" in value or ":" in value or ".." in value.split("/"):
        raise ValueError("Only relative public deliverable/input paths are supported")
    return value


def _number(value: str, kind: str) -> int | float:
    number = json.loads(value.strip())
    if type(number) not in (int, float) or (kind == "int" and type(number) is not int):
        raise ValueError("Invalid typed numeric range in public spec")
    return number


def _spec5(spec: _Spec) -> tuple[dict, list[str], dict]:
    spec.find(r"# SPEC5: .+ Configuration System — Full Specification")
    start, end = spec.section("## Configuration Schema")
    config, refs = {}, {}
    for line_number in range(start + 1, end + 1):
        line = spec.lines[line_number - 1]
        if not line.startswith("| `"):
            continue
        cells = [x.strip() for x in line.strip("|").split("|")]
        if len(cells) != 6:
            raise ValueError("Unsupported SPEC5 schema table row")
        key, kind, raw_default, env, validation, _ = cells
        key, kind, env = (x.strip("`") for x in (key, kind, env))
        if key in config or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env):
            raise ValueError("Duplicate or invalid SPEC5 key/environment mapping")
        default = json.loads(raw_default)
        expected_types = {"string": (str,), "enum": (str,), "bool": (bool,), "int": (int,), "float": (int, float)}
        if kind not in expected_types or type(default) not in expected_types[kind]:
            raise ValueError("Invalid SPEC5 default type")
        entry: dict[str, Any] = {"type": kind, "default": default, "env_var": env}
        if kind in ("int", "float"):
            match = re.fullmatch(rf"{kind} in range \[([^,]+),\s*([^\]]+)\]", validation)
            if not match:
                raise ValueError("Missing SPEC5 numeric bounds")
            minimum, maximum = (_number(x, kind) for x in match.groups())
            if not minimum <= default <= maximum:
                raise ValueError("SPEC5 default lies outside declared bounds")
            _, rule = spec.find(rf"- `{re.escape(key)}`: must be in range \[([^,]+),\s*([^\]]+)\] \(inclusive\)")
            if [_number(x, kind) for x in rule.groups()] != [minimum, maximum]:
                raise ValueError("Conflicting SPEC5 range table and validation rule")
            entry["constraints"] = {"min": minimum, "max": maximum, "inclusive": True}
        elif kind == "enum":
            match = re.fullmatch(r"one of (\[.+\])", validation)
            if not match:
                raise ValueError("Missing SPEC5 enum values")
            allowed = ast.literal_eval(match.group(1))
            if type(allowed) is not list or not allowed or any(type(v) is not str for v in allowed) or default not in allowed:
                raise ValueError("Invalid SPEC5 enum values")
            _, rule = spec.find(rf"- `{re.escape(key)}`: must be one of (\[.+\]) \(case-sensitive\)")
            if ast.literal_eval(rule.group(1)) != allowed:
                raise ValueError("Conflicting SPEC5 enum table and validation rule")
            entry["constraints"] = {"allowed": allowed, "case_sensitive": True}
        elif kind == "string":
            if validation not in ("non-empty string", "string (any)"):
                raise ValueError("Unsupported SPEC5 string rule")
            entry["constraints"] = {"non_empty": validation == "non-empty string"}
            if validation == "non-empty string":
                spec.find(rf"- `{re.escape(key)}`: must be a non-empty string")
        elif not validation.startswith("bool"):
            raise ValueError("Unsupported SPEC5 boolean rule")
        # The separate environment mapping is a consistency check, not a second
        # information source. Do not silently prefer one conflicting table.
        spec.find(rf"\| `{re.escape(env)}` \| `{re.escape(key)}` \| `{kind}` \|")
        config[key] = entry
        refs[f"facts.config.{key}"] = _ref(line_number)
    if not config or len({entry["env_var"] for entry in config.values()}) != len(config):
        raise ValueError("Empty schema or duplicate environment mapping")
    env_start, env_end = spec.section("## Environment Variable Mapping")
    env_rows = []
    for line in spec.lines[env_start:env_end]:
        if line.startswith("| `"):
            match = re.fullmatch(r"\| `([^`]+)` \| `([^`]+)` \| `([^`]+)` \|", line)
            if not match:
                raise ValueError("Unsupported SPEC5 environment table")
            env_rows.append(match.groups())
    wanted_rows = [(v["env_var"], k, v["type"]) for k, v in config.items()]
    if sorted(env_rows) != sorted(wanted_rows):
        raise ValueError("SPEC5 schema and environment table do not cover the same facts")

    priority_start, priority_end = spec.section("## Priority Cascade")
    rank_patterns = {
        "cli_args": r"(\d+)\. \*\*CLI arguments\*\* .*",
        "env_vars": r"(\d+)\. \*\*Environment variables\*\* .*",
        "config_file": r"(\d+)\. \*\*Config file\*\* .*",
        "defaults": r"(\d+)\. \*\*Built-in defaults\*\* .*",
    }
    priority = {key: int(spec.find(pattern)[1].group(1)) for key, pattern in rank_patterns.items()}
    if sorted(priority.values()) != [1, 2, 3, 4]:
        raise ValueError("Invalid SPEC5 priority ranks")
    bool_line, bool_match = spec.find(r"- `bool`: accept (.+);")
    tokens = re.findall(r"`([^`]+)`", bool_match.group(1))
    if tokens != ["true", "false", "1", "0", "yes", "no", "on", "off"] or "case-insensitive" not in bool_match.group(1):
        raise ValueError("Unsupported SPEC5 boolean coercion wording")
    for key, entry in config.items():
        if entry["type"] == "bool":
            spec.find(rf"- `{re.escape(key)}`: accepts true/false \(case-insensitive\), 1/0, yes/no, on/off as string inputs")
    error_line, exception = spec.find(r"class (\w+)\((\w+)\):")
    error_type, error_base = exception.groups()
    spec.contains(f"All validation failures must raise `{error_type}` (a subclass of `{error_base}`)")
    spec.contains("The error must include the key name and the invalid value.")
    spec.contains(f"- `int`: parse as integer; raise `{error_type}` if not parseable")
    spec.contains(f"- `float`: parse as float; raise `{error_type}` if not parseable")
    spec.contains("- `string`: use as-is")
    spec.contains("env_vars: dict | None = None,   # defaults to os.environ if None")
    spec.contains("A key set to the string `\"\"` in a lower-priority source is still overridden")
    spec.contains("by a non-None value from a higher-priority source.")
    unknown_line = spec.contains("Unknown keys in the config file are ignored (not an error).")
    spec.contains("even if no source provides a value (use the default).")
    spec.contains("FileNotFoundError: If config_file is specified but does not exist.")
    module_line, module = spec.find(r"# ([a-zA-Z0-9_]+\.py) — you must implement this file")
    for phrase in ("def load_config(", "def get_schema() -> dict:", "def validate_value(key: str, value) -> object:"):
        spec.contains(phrase)
    behavior = {
        "priority_rank": priority, "env_fallback_when_none": "os.environ",
        "bool_string_tokens": tokens, "bool_case_sensitive": False,
        "invalid_int_raises_validation_error": True, "invalid_float_raises_validation_error": True,
        "strings_preserved": True, "validation_error_includes_key": True,
        "validation_error_includes_invalid_value": True, "missing_file_error": "FileNotFoundError",
        "unknown_file_keys_ignored": True, "return_all_keys_with_defaults": True,
        "higher_priority_non_none_overrides_empty_string": True,
    }
    api = {"exception": {"name": error_type, "base": error_base},
           "functions": {"load_config": ["config_file", "env_vars", "cli_args"],
                         "get_schema": [], "validate_value": ["key", "value"]}}
    deliverables = [_path(module.group(1))]
    refs.update({"facts.behavior": _ref(bool_line, len(spec.lines)),
                 "facts.behavior.priority_rank": _ref(priority_start, priority_end),
                 "facts.behavior.unknown_file_keys_ignored": _ref(unknown_line),
                 "facts.api": _ref(error_line, spec.contains("## Environment Variable Mapping") - 1),
                 "facts.deliverables": _ref(module_line)})
    return {"config": config, "behavior": behavior, "api": api}, deliverables, refs


def _int1(spec: _Spec) -> tuple[dict, list[str], dict]:
    spec.find(r"# INT1: Multi-Service Pipeline Repair")
    collector_start = spec.contains("### Collector")
    processor_start = spec.contains("### Processor")
    reporter_start = spec.contains("### Reporter")
    pipeline_start = spec.contains("### Pipeline")
    deliverable_start = spec.contains("## Deliverables")
    _, input_match = spec.find(r"- Reads `([^`]+)`")
    outputs = [(i, re.fullmatch(r"- Output file: `([^`]+)`", line)) for i, line in enumerate(spec.lines, 1)]
    outputs = [(i, m.group(1)) for i, m in outputs if m]
    if len(outputs) != 3:
        raise ValueError("INT1 must declare collector/processor/reporter output files")
    if not (collector_start < outputs[0][0] < processor_start < outputs[1][0]
            < reporter_start < outputs[2][0] < pipeline_start):
        raise ValueError("INT1 output files must belong to their declared stages")
    spec.contains("A single JSON array containing all records (not newline-delimited JSON)")
    _, collected_fields = spec.find(r"- Each record must include: (.+)")
    field_parts = collected_fields.group(1).split(", ")
    parsed_fields = [re.fullmatch(r"`([^`]+)` \((string|integer)\)", part) for part in field_parts]
    if len(parsed_fields) != 4 or not all(parsed_fields):
        raise ValueError("Unsupported INT1 collector field declaration")
    fields = dict(m.groups() for m in parsed_fields)
    if len(fields) != 4:
        raise ValueError("Duplicate INT1 collector field")
    spec.contains("- **Input**: The JSON array produced by the collector")
    spec.contains("Email addresses with a `+` character in the local part")
    _, score = spec.find(r"  - Score must be an integer between (\d+) and (\d+) inclusive")
    if int(score.group(1)) > int(score.group(2)):
        raise ValueError("Inverted INT1 score bounds")
    spec.contains("  - Name must be non-empty")
    _, output_name = spec.find(r"- \*\*Output field naming\*\*: The output record must use the field name `([^`]+)` \(not .+\)")
    _, valid_fields = spec.find(r"- Each valid output record must include: (.+)")
    processed_fields = re.findall(r"`([^`]+)`", valid_fields.group(1))
    if len(processed_fields) != 4 or not valid_fields.group(1).endswith("`processed_at` (ISO timestamp string)"):
        raise ValueError("Unsupported INT1 processed field declaration")
    if len(set(processed_fields)) != 4 or output_name.group(1) not in processed_fields:
        raise ValueError("Conflicting INT1 processed field names")
    _, rejected = spec.find(r"- Records that fail validation must be written to `([^`]+)` \(one JSON object per line\) — they must not be silently dropped")
    spec.contains("- **Input**: The list of processed records from the processor")
    _, templates = spec.find(r"- Templates must reference (.+) — not any aliased or renamed fields")
    template_fields = re.findall(r"`([^`]+)`", templates.group(1))
    if len(template_fields) != 3 or any(not f.startswith("record.") or f[7:] not in processed_fields for f in template_fields):
        raise ValueError("Conflicting INT1 reporter fields")
    _, sorting = spec.find(r"- Records in the report must appear sorted by (\w+) in descending order")
    _, stages = spec.find(r"- Orchestrates the three stages: (.+)")
    stage_order = stages.group(1).split(" → ")
    if len(stage_order) != 3 or any(not s.isidentifier() for s in stage_order):
        raise ValueError("Unsupported INT1 stage order")
    spec.contains(f"- Records rejected during processing must be logged to `{rejected.group(1)}`, not silently dropped")
    _, counts = spec.find(r"- End-to-end: (\d+) input records → (\d+) valid output records \((\d+) records in the input have genuinely invalid data\)")
    input_count, valid_count, invalid_count = map(int, counts.groups())
    if valid_count + invalid_count != input_count:
        raise ValueError("Inconsistent public INT1 record counts")
    spec.contains("- Fixed pipeline that passes integration test")
    deliverables = []
    for line in spec.lines[deliverable_start:]:
        match = re.fullmatch(r"- `([^`]+)` with (.+)", line)
        if match:
            deliverables.append(_path(match.group(1)))
    if deliverables != [outputs[1][1], rejected.group(1), outputs[2][1]]:
        raise ValueError("Inconsistent INT1 deliverable paths")
    spec.contains(f"- `{outputs[1][1]}` with {valid_count} records")
    spec.contains(f"- `{rejected.group(1)}` with {invalid_count} error entries")
    facts = {
        "collector": {"input_path": _path(input_match.group(1)), "output_path": _path(outputs[0][1]),
                      "output_is_json_array": True, "fields": fields},
        "processor": {"input_is_json_array": True, "email_allow_plus_local_part": True,
                      "score_type": "integer", "score_min": int(score.group(1)), "score_max": int(score.group(2)),
                      "score_bounds_inclusive": True, "name_non_empty": True,
                      "output_name_field": output_name.group(1), "output_fields": processed_fields,
                      "processed_at_format": "ISO timestamp string", "output_path": _path(outputs[1][1]),
                      "invalid_path": _path(rejected.group(1)), "invalid_json_object_per_line": True,
                      "no_silent_drop": True},
        "reporter": {"input_is_list": True, "template_fields": template_fields,
                     "output_path": _path(outputs[2][1]), "sort_key": sorting.group(1), "descending": True},
        "pipeline": {"stage_order": stage_order, "input_records": input_count,
                     "valid_records": valid_count, "invalid_records": invalid_count,
                     "integration_test_required": True},
    }
    refs = {"facts.collector": _ref(collector_start, processor_start - 1),
            "facts.processor": _ref(processor_start, reporter_start - 1),
            "facts.reporter": _ref(reporter_start, pipeline_start - 1),
            "facts.pipeline": _ref(pipeline_start, deliverable_start + 1),
            "facts.deliverables": _ref(deliverable_start, len(spec.lines))}
    return facts, deliverables, refs


def build_contract(task_id: str, spec_text: str) -> HandoffContract:
    """Extract supported finite facts from caller-authorized public spec text.

    The exact supplied UTF-8 text determines source_spec_sha256. Whitespace or
    source changes require a newly submitted hash; unknown task formats reject.
    """
    if type(task_id) is not str:
        raise ValueError("task_id must be a string")
    if type(spec_text) is not str or not spec_text:
        raise ValueError("spec_text must contain authorized public specification text")
    builders = {"SPEC5_config_system": _spec5, "INT1_pipeline_repair": _int1}
    if task_id not in builders:
        raise ValueError(f"Unsupported task contract: {task_id!r}")
    facts, deliverables, refs = builders[task_id](_Spec(spec_text))
    return HandoffContract(task_id, hashlib.sha256(spec_text.encode("utf-8")).hexdigest(), facts, deliverables, refs)
