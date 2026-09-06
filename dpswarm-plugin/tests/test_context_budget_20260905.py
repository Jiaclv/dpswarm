"""Context budgets apply to the complete rendered package, not material bodies."""
import json

import pytest

from dpswarm.context.assembler import AssemblerBrief, ContextAssembler, est_tokens
from dpswarm.context.memory import MemoryService
from dpswarm.types import ModelRoute


ROUTE = ModelRoute("mock", "b-kimi")


def test_oversized_compressor_output_falls_back_without_truncating_required_material(tmp_path):
    original = "CRITICAL_START:" + "a" * 1200 + ":CRITICAL_END"
    calls = []

    def compress(materials, brief):
        calls.append(materials)
        return "x" * 6000

    assembler = ContextAssembler(MemoryService(), artifacts={"sample": original}, compress_fn=compress)
    brief = AssemblerBrief(task_intent="fixture", select=["sample"], token_budget=100)
    package = assembler.assemble(brief, ROUTE, heterogeneous=True)
    assert len(calls) == 1
    assert est_tokens(package.content) <= 100
    assert "x" * 100 not in package.content
    assert "CRITICAL_START" not in package.content  # No partial required body.
    assert [(entry.ref, entry.required) for entry in package.entries] == [("sample", False)]
    assert package.source_pointers == ["sample"]
    assert assembler.artifacts["sample"] == original
    reference, _ = assembler.write_package(package, tmp_path / "package")
    manifest = json.loads(next((tmp_path / "package").glob("*.manifest.json")).read_text(encoding="utf-8"))
    assert manifest["est_tokens"] <= 100
    assert manifest["source_pointers"] == ["sample"]
    assert reference.endswith(".md")


def test_fitting_summary_accounts_for_reference_and_framework_overhead():
    memory = MemoryService()
    entry = memory.add_candidate("stable fact " * 10, "team", ["source"])
    memory.promote(entry.memory_id)
    original = "file details " * 1000
    assembler = ContextAssembler(memory, artifacts={"file": original},
                                 compress_fn=lambda materials, brief: "Complete concise summary.")
    brief = AssemblerBrief(task_intent="Review the file", select=[], token_budget=220)
    package = assembler.assemble(brief, ROUTE, heterogeneous=True)
    assert est_tokens(package.content) <= 220
    assert "Complete concise summary." in package.content
    assert any(entry.ref == "summary:team" and entry.required for entry in package.entries)
    assert set(package.source_pointers) == {"memory:" + entry.memory_id, "file"}
    assert memory.get(entry.memory_id).content == "stable fact " * 10
    assert assembler.artifacts["file"] == original
    for item in package.entries:
        if item.required and item.ref.startswith("memory:"):
            assert memory.get(item.ref.split(":", 1)[1]).content in package.content


@pytest.mark.parametrize("heterogeneous", [False, True])
def test_raw_material_fits_but_rendered_overhead_requires_whole_entry_selection(heterogeneous):
    original = "required beginning " + "data " * 100 + "required conclusion"
    assembler = ContextAssembler(MemoryService(), artifacts={"sample": original})
    unconstrained = assembler.assemble(AssemblerBrief("Task", ["sample"], token_budget=2000),
                                       ROUTE, heterogeneous)
    target = est_tokens(unconstrained.content) - 1
    assert est_tokens(original) < target
    package = assembler.assemble(AssemblerBrief("Task", ["sample"], token_budget=target),
                                 ROUTE, heterogeneous)
    assert est_tokens(package.content) <= target
    assert package.source_pointers == ["sample"]
    assert not package.entries[0].required
    assert "required beginning" not in package.content
    assert assembler.artifacts["sample"] == original


def test_summary_that_only_fits_without_framework_is_rejected_as_a_whole():
    summary = "summary-content " * 15
    assembler = ContextAssembler(MemoryService(), artifacts={"sample": "a" * 6000},
                                 compress_fn=lambda materials, brief: summary)
    assert est_tokens(summary) < 120
    package = assembler.assemble(AssemblerBrief("Task", ["sample"], token_budget=120), ROUTE, True)
    assert est_tokens(package.content) <= 120
    assert "summary-content" not in package.content
    assert not any(entry.ref.startswith("summary:") for entry in package.entries)
    assert package.entries[0].required is False


@pytest.mark.parametrize("budget", [0, -1, True, 100.5, None])
def test_invalid_budget_fails_before_compression_or_package_write(tmp_path, budget):
    calls = []
    assembler = ContextAssembler(MemoryService(), artifacts={"file": "a" * 3000},
                                 compress_fn=lambda *args: calls.append(args) or "summary")
    target = tmp_path / "package"
    with pytest.raises(ValueError, match="CONTEXT_BUDGET_INVALID"):
        package = assembler.assemble(AssemblerBrief("Task", ["file"], token_budget=budget), ROUTE, True)
        assembler.write_package(package, target)
    assert calls == []
    assert not target.exists()


@pytest.mark.parametrize("large_part", ["intent", "references"])
def test_unshrinkable_framework_fails_before_compression_or_write(tmp_path, large_part):
    calls = []
    artifacts = ({"sample": "a" * 3000} if large_part == "intent" else
                 {"file-" + str(n) + "x" * 120: "content" for n in range(10)})
    task = "Task must remain complete. " * 100 if large_part == "intent" else "Task"
    assembler = ContextAssembler(MemoryService(), artifacts=artifacts,
                                 compress_fn=lambda *args: calls.append(args) or "summary")
    target = tmp_path / "package"
    with pytest.raises(ValueError, match="CONTEXT_BUDGET_TOO_SMALL"):
        package = assembler.assemble(AssemblerBrief(task, [], token_budget=100), ROUTE, True)
        assembler.write_package(package, target)
    assert calls == []
    assert not target.exists()
    assert assembler.artifacts == artifacts


def test_empty_materials_still_account_for_task_and_framework():
    assembler = ContextAssembler(MemoryService())
    with pytest.raises(ValueError, match="CONTEXT_BUDGET_TOO_SMALL"):
        assembler.assemble(AssemblerBrief("long task " * 100, [], token_budget=30), ROUTE, False)


def test_fitting_package_retains_full_required_contents_and_does_not_compress():
    calls = []
    artifacts = {"first": "First complete fact.", "second": "Second complete fact."}
    assembler = ContextAssembler(MemoryService(), artifacts=artifacts,
                                 compress_fn=lambda *args: calls.append(args) or "summary")
    package = assembler.assemble(AssemblerBrief("Task", [], token_budget=500, inline_token_limit=10),
                                 ROUTE, False)
    assert calls == []
    assert est_tokens(package.content) <= 500
    assert [entry.ref for entry in package.entries] == ["first", "second"]
    assert all(entry.required and not entry.inline for entry in package.entries)
    assert all(value in package.content for value in artifacts.values())


def test_many_short_materials_include_all_rendering_overhead():
    artifacts = {"file-" + str(n): "ab" for n in range(12)}
    assembler = ContextAssembler(MemoryService(), artifacts=artifacts)
    package = assembler.assemble(AssemblerBrief("Task", [], token_budget=230), ROUTE, True)
    assert est_tokens(package.content) <= 230
    assert set(package.source_pointers) == set(artifacts)
    assert {entry.ref for entry in package.entries} == set(artifacts)
