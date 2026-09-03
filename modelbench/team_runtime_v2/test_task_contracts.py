"""Offline contract tests using public specs and already-recorded P messages."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from modelbench.team_runtime_v2.task_contracts import build_contract


EVAL = Path(__file__).resolve().parents[1] / "team_eval20260903"
WEB_SPEC = EVAL / "instances/SPEC5_config_system/task/spec.md"
STATIC_SPEC = EVAL / "TeamBench/tasks/SPEC5_config_system/spec.md"
INT_SPEC = EVAL / "instances/INT1_pipeline_repair/task/spec.md"


def _entry(kind, default, env, constraints=None):
    entry = {"type": kind, "default": default, "env_var": env}
    if constraints is not None:
        entry["constraints"] = constraints
    return entry


def _range(low, high):
    return {"min": low, "max": high, "inclusive": True}


# Manually transcribed public schema values; these are handoff data, not a task
# implementation or values obtained from the grader/expected.json.
WEB_CONFIG = {
    "host": _entry("string", "0.0.0.0", "WEB_HOST", {"non_empty": True}),
    "port": _entry("int", 6155, "WEB_PORT", _range(2048, 49151)),
    "log_level": _entry("enum", "WARN", "WEB_LOG_LEVEL", {"allowed": ["INFO", "WARN", "ERROR"], "case_sensitive": True}),
    "request_timeout": _entry("int", 120, "WEB_REQUEST_TIMEOUT", _range(1, 3600)),
    "max_connections": _entry("int", 348, "WEB_MAX_CONNECTIONS", _range(1, 1000)),
    "debug_mode": _entry("bool", False, "WEB_DEBUG"),
    "static_dir": _entry("string", "./static", "WEB_STATIC_DIR", {"non_empty": True}),
    "cors_origins": _entry("string", "*", "WEB_CORS_ORIGINS", {"non_empty": False}),
    "keep_alive_timeout": _entry("int", 10, "WEB_KEEP_ALIVE_TIMEOUT", _range(1, 300)),
    "ssl_enabled": _entry("bool", False, "WEB_SSL_ENABLED"),
}
STATIC_CONFIG = {
    "queue_url": _entry("string", "redis://localhost:6379/0", "CELERY_QUEUE_URL", {"non_empty": True}),
    "concurrency": _entry("int", 3, "CELERY_CONCURRENCY", _range(1, 32)),
    "max_retries": _entry("int", 8, "CELERY_MAX_RETRIES", _range(0, 20)),
    "retry_backoff_seconds": _entry("int", 1, "CELERY_RETRY_BACKOFF", _range(1, 300)),
    "job_timeout": _entry("int", 300, "CELERY_JOB_TIMEOUT", _range(1, 3600)),
    "log_level": _entry("enum", "INFO", "CELERY_LOG_LEVEL", {"allowed": ["DEBUG", "INFO", "WARN"], "case_sensitive": True}),
    "dead_letter_queue": _entry("bool", True, "CELERY_DEAD_LETTER"),
    "heartbeat_interval": _entry("int", 60, "CELERY_HEARTBEAT", _range(5, 300)),
    "prefetch_count": _entry("int", 10, "CELERY_PREFETCH", _range(1, 100)),
    "ack_on_failure": _entry("bool", False, "CELERY_ACK_ON_FAILURE"),
    "metrics_enabled": _entry("bool", True, "CELERY_METRICS"),
}
BEHAVIOR = {
    "priority_rank": {"cli_args": 1, "env_vars": 2, "config_file": 3, "defaults": 4},
    "env_fallback_when_none": "os.environ",
    "bool_string_tokens": ["true", "false", "1", "0", "yes", "no", "on", "off"],
    "bool_case_sensitive": False, "invalid_int_raises_validation_error": True,
    "invalid_float_raises_validation_error": True, "strings_preserved": True,
    "validation_error_includes_key": True, "validation_error_includes_invalid_value": True,
    "missing_file_error": "FileNotFoundError", "unknown_file_keys_ignored": True,
    "return_all_keys_with_defaults": True, "higher_priority_non_none_overrides_empty_string": True,
}
API = {"exception": {"name": "ConfigValidationError", "base": "ValueError"},
       "functions": {"load_config": ["config_file", "env_vars", "cli_args"],
                     "get_schema": [], "validate_value": ["key", "value"]}}


def _payload(contract, facts=None):
    return {"task_id": contract.task_id, "source_spec_sha256": contract.source_spec_sha256,
            "source_ref": "spec", "facts": deepcopy(facts if facts is not None else contract.required_facts),
            "assumptions": [], "unresolved": []}


@pytest.fixture
def web_contract():
    return build_contract("SPEC5_config_system", WEB_SPEC.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path,expected", [(WEB_SPEC, WEB_CONFIG), (STATIC_SPEC, STATIC_CONFIG)])
def test_generated_and_static_spec5_all_public_schema_fields(path, expected):
    text = path.read_text(encoding="utf-8")
    c = build_contract("SPEC5_config_system", text)
    assert c.required_facts == {"config": expected, "behavior": BEHAVIOR, "api": API,
                                "deliverables": ["config_system.py"]}
    assert c.source_spec_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    for key in expected:
        ref = c.source_refs[f"facts.config.{key}"]
        assert f"`{key}`" in text.splitlines()[ref["start_line"] - 1]


@pytest.mark.parametrize("parts", [
    ("config", "debug_mode", "env_var"),
    ("config", "keep_alive_timeout", "constraints", "max"),
    ("config", "keep_alive_timeout", "constraints", "min"),
    ("config", "static_dir", "default"),
    ("config", "log_level", "constraints", "allowed"),
])
def test_missing_critical_field_rejected(web_contract, parts):
    submitted = _payload(web_contract)
    target = submitted["facts"]
    for key in parts[:-1]:
        target = target[key]
    del target[parts[-1]]
    verdict = web_contract.validate(submitted)
    assert not verdict["ok"]
    assert "facts." + ".".join(parts) in verdict["missing"]


@pytest.mark.parametrize("parts,value", [
    (("config", "debug_mode", "env_var"), "WEB_DEBUG_MODE"),
    (("config", "keep_alive_timeout", "constraints", "min"), 0),
    (("config", "keep_alive_timeout", "constraints", "max"), 3600),
    (("config", "debug_mode", "default"), 0),
    (("config", "port", "default"), "6155"),
    (("behavior", "priority_rank", "cli_args"), 4),
])
def test_observed_errors_and_strict_types_rejected(web_contract, parts, value):
    submitted = _payload(web_contract)
    target = submitted["facts"]
    for key in parts[:-1]:
        target = target[key]
    target[parts[-1]] = value
    assert not web_contract.validate(submitted)["ok"]


OLD_PLANS = [
    ("", f"SPEC5_config_system__team__{model}")
    for model in ("glm-5.3", "glm-5.3-flash", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
] + [("native_followup", f"SPEC5_config_system__native_team__{model}") for model in ("glm-5.3", "glm-5.3-flash")]


@pytest.mark.parametrize("subdir,run_id", OLD_PLANS)
def test_all_seven_recorded_prose_plans_are_not_structured_acceptance(web_contract, subdir, run_id):
    path = EVAL / subdir / "results" / run_id / "messages/dialogue.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    plans = [m["content"] for m in records if m["role"] == "planner" and m["to"] == "executor"]
    assert len(plans) == 1
    assert not web_contract.validate(plans[0])["ok"]
    submitted = _payload(web_contract)
    submitted["facts"] = plans[0]
    assert not web_contract.validate(submitted)["ok"]


@pytest.mark.parametrize("executor", ["gpt-5.6-terra", "gpt-5.6-luna"])
def test_complete_old_plans_can_be_manually_reexpressed_as_correct_package(web_contract, executor):
    # The earlier prose itself is never auto-accepted. This explicit test fixture
    # transcribes the public facts into the new representation without solving
    # config_system.py or consulting hidden expected answers.
    facts = {"config": deepcopy(WEB_CONFIG), "behavior": deepcopy(BEHAVIOR),
             "api": deepcopy(API), "deliverables": ["config_system.py"]}
    old = EVAL / "results" / f"SPEC5_config_system__team__{executor}" / "messages/dialogue.jsonl"
    assert old.is_file()
    submitted = _payload(web_contract, facts)
    before = deepcopy(submitted)
    assert web_contract.accepted_package(submitted)["facts"] == facts
    assert submitted == before


def test_changed_public_value_is_parsed_not_hardcoded(web_contract):
    text = WEB_SPEC.read_text(encoding="utf-8").replace("WEB_DEBUG", "WEB_DIAGNOSTIC")
    c = build_contract("SPEC5_config_system", text)
    assert c.required_facts["config"]["debug_mode"]["env_var"] == "WEB_DIAGNOSTIC"
    assert c.source_spec_sha256 != web_contract.source_spec_sha256
    assert not c.validate(_payload(web_contract))["ok"]


def test_conflicting_public_tables_fail_closed():
    text = WEB_SPEC.read_text(encoding="utf-8").replace("| `WEB_DEBUG` | `debug_mode`", "| `WEB_DIAGNOSTIC` | `debug_mode`")
    with pytest.raises(ValueError, match="Unsupported public spec"):
        build_contract("SPEC5_config_system", text)


def test_missing_schema_row_cannot_silently_shrink_contract():
    text = WEB_SPEC.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.startswith("| `debug_mode` |"))
    with pytest.raises(ValueError, match="environment table"):
        build_contract("SPEC5_config_system", text)


def test_int1_public_interface_counts_and_deliverables():
    c = build_contract("INT1_pipeline_repair", INT_SPEC.read_text(encoding="utf-8"))
    facts = c.required_facts
    assert facts["collector"] == {"input_path": "data/input.csv", "output_path": "data/collected.json",
                                   "output_is_json_array": True,
                                   "fields": {"name": "string", "email": "string", "score": "integer", "raw_line": "integer"}}
    assert facts["processor"] == {"input_is_json_array": True, "email_allow_plus_local_part": True,
        "score_type": "integer", "score_min": 0, "score_max": 100, "score_bounds_inclusive": True,
        "name_non_empty": True, "output_name_field": "name", "output_fields": ["name", "email", "score", "processed_at"],
        "processed_at_format": "ISO timestamp string", "output_path": "data/processed.json",
        "invalid_path": "data/errors.jsonl", "invalid_json_object_per_line": True, "no_silent_drop": True}
    assert facts["reporter"] == {"input_is_list": True, "template_fields": ["record.name", "record.email", "record.score"],
                                  "output_path": "data/report.txt", "sort_key": "score", "descending": True}
    assert facts["pipeline"] == {"stage_order": ["collector", "processor", "reporter"], "input_records": 20,
                                  "valid_records": 18, "invalid_records": 2, "integration_test_required": True}
    assert c.deliverables == ["data/processed.json", "data/errors.jsonl", "data/report.txt"]
    # Grader-only byte limits and fixed reference names are intentionally absent.
    assert "10000" not in json.dumps(facts)
    assert "Alice" not in json.dumps(facts)
    bad = _payload(c)
    bad["facts"]["deliverables"].pop()
    assert not c.validate(bad)["ok"]


def test_builder_has_no_filesystem_access(monkeypatch):
    text = WEB_SPEC.read_text(encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("No file access is authorized during contract construction")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert build_contract("SPEC5_config_system", text).deliverables == ["config_system.py"]


def test_unsupported_task_and_incomplete_spec_rejected():
    with pytest.raises(ValueError, match="Unsupported task"):
        build_contract("DIST1_queue_race", "public text")
    with pytest.raises(ValueError, match="Unsupported public spec"):
        build_contract("SPEC5_config_system", "# SPEC5: brief without full schema")
