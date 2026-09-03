from copy import deepcopy

import pytest

from dpswarm.team_runtime.handoff import HandoffContract


@pytest.fixture
def contract():
    return HandoffContract(
        "public_demo", "a" * 64,
        {"config": {"enabled": {"default": False}, "limit": {"max": 10}}},
        ["out.json"], {"facts.config": {"source_ref": "spec", "start_line": 1, "end_line": 3}},
    )


def payload(contract):
    return {"task_id": contract.task_id, "source_spec_sha256": contract.source_spec_sha256,
            "source_ref": "spec", "facts": contract.required_facts,
            "assumptions": [], "unresolved": []}


def test_acceptance_is_a_deep_copy_and_does_not_mutate_input(contract):
    submitted = payload(contract)
    before = deepcopy(submitted)
    package = contract.accepted_package(submitted)
    assert submitted == before
    assert package["handoff_validated"] is True
    assert package["source_refs"]["facts.config"]["source_ref"] == "spec"
    package["facts"]["config"]["limit"]["max"] = 999
    package["source_refs"]["facts.config"]["start_line"] = 999
    assert contract.validate(submitted) == {"ok": True, "errors": [], "missing": []}
    assert contract.required_facts["config"]["limit"]["max"] == 10
    assert contract.source_refs["facts.config"]["start_line"] == 1


def test_exposed_contract_copies_cannot_rewrite_expected_facts(contract):
    facts = contract.required_facts
    facts["config"]["enabled"]["default"] = True
    assert contract.required_facts["config"]["enabled"]["default"] is False
    assert not contract.validate({**payload(contract), "facts": facts})["ok"]


@pytest.mark.parametrize("value", [0, "false", None])
def test_boolean_requires_boolean_not_falsy_equivalent(contract, value):
    submitted = payload(contract)
    submitted["facts"]["config"]["enabled"]["default"] = value
    verdict = contract.validate(submitted)
    assert not verdict["ok"]
    assert "facts.config.enabled.default: incorrect JSON type" in verdict["errors"]


@pytest.mark.parametrize("value", [True, 10.0, "10", 11])
def test_integer_exact_type_and_value(contract, value):
    submitted = payload(contract)
    submitted["facts"]["config"]["limit"]["max"] = value
    assert not contract.validate(submitted)["ok"]


def test_missing_field_returns_path_without_expected_answer(contract):
    submitted = payload(contract)
    del submitted["facts"]["config"]["limit"]["max"]
    verdict = contract.validate(submitted)
    assert verdict == {"ok": False, "errors": [], "missing": ["facts.config.limit.max"]}
    with pytest.raises(ValueError, match="Handoff rejected"):
        contract.accepted_package(submitted)


@pytest.mark.parametrize("change", [
    {"task_id": "another_task"}, {"source_spec_sha256": "b" * 64},
    {"source_ref": "grader/expected.json"}, {"unresolved": ["missing env map"]},
    {"unresolved": "none"}, {"assumptions": [False]}, {"private_answer": "anything"},
])
def test_bad_envelope_or_unresolved_rejected(contract, change):
    assert not contract.validate({**payload(contract), **change})["ok"]


def test_unknown_fact_and_injected_source_reference_rejected(contract):
    submitted = payload(contract)
    submitted["facts"]["grader_pass"] = True
    submitted["source_refs"] = {"facts.config": "/grader/expected.json"}
    verdict = contract.validate(submitted)
    assert not verdict["ok"]
    assert "facts.grader_pass: unknown field" in verdict["errors"]
    assert "source_refs: unknown field" in verdict["errors"]


def test_tool_schema_is_structural_not_an_answer_filled_const(contract):
    tool = contract.tool_declaration()
    assert tool["function"]["name"] == "submit_handoff"
    params = tool["function"]["parameters"]
    assert params["additionalProperties"] is False
    assert params["properties"]["facts"]["properties"]["config"]["properties"]["limit"]["properties"]["max"] == {"type": "integer"}
    assert "Executor cannot read the full spec" in contract.prompt_instructions()
    assert contract.source_spec_sha256 in contract.prompt_instructions()


def test_contract_constructor_rejects_nonpublic_source():
    with pytest.raises(ValueError, match="public spec"):
        HandoffContract("task", "a" * 64, {"x": 1}, ["x.py"],
                        {"facts.x": {"source_ref": "expected", "start_line": 1, "end_line": 1}})


def test_constructor_copies_supplied_facts():
    facts = {"x": 1}
    c = HandoffContract("task", "a" * 64, facts, ["x.py"],
                        {"facts.x": {"source_ref": "spec", "start_line": 1, "end_line": 1}})
    facts["x"] = 2
    assert c.required_facts["x"] == 1
