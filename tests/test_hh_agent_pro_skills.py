from types import SimpleNamespace

import pytest

from hh_agent_pro.runtime import HH
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.skills import SkillLearner, SkillStore, SkillValidationError
from hh_agent_pro.workspace import WorkspaceContext


class FinalModel:
    supports_prompt_cache = False
    last_completion_metadata = {"model": "skill-test"}

    def complete(self, prompt, max_new_tokens, **kwargs):
        return "<final>ok</final>"


def make_agent(root):
    (root / "README.md").write_text("skills\n", encoding="utf-8")
    return HH(
        model_client=FinalModel(),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".hh" / "sessions"),
        approval_policy="auto",
    )


def test_skill_store_crud_and_security(tmp_path):
    store = SkillStore(tmp_path / ".hh" / "skills")
    store.create("release-check", "Verify a release", "# Release\n\nMARKER_BODY\n")

    assert [skill.name for skill in store.list()] == ["release-check"]
    assert "MARKER_BODY" in store.read("release-check")
    store.patch("release-check", "MARKER_BODY", "Run pytest, then publish.")
    assert "Run pytest" in store.read("release-check")

    with pytest.raises(SkillValidationError):
        store.create("bad-secret", "bad", "api_key = sk-abcdefghijklmnop")
    with pytest.raises(SkillValidationError):
        store.create("Bad_Name", "bad", "# bad")

    store.delete("release-check")
    assert store.list() == []


def test_prompt_injects_only_skill_index_and_skill_view_loads_body(tmp_path):
    store = SkillStore(tmp_path / ".hh" / "skills")
    store.create("release-check", "Verify a release", "# Release\n\nPRIVATE_PROCEDURE_MARKER\n")
    agent = make_agent(tmp_path)

    prompt, metadata = agent.context_manager.build("发布版本")
    assert "release-check: Verify a release" in prompt
    assert "PRIVATE_PROCEDURE_MARKER" not in prompt
    assert metadata["skills"]["available_count"] == 1

    result = agent.execute_tool("skill_view", {"name": "release-check"})
    assert result.metadata["tool_status"] == "ok"
    assert "PRIVATE_PROCEDURE_MARKER" in result.content


def test_auto_learner_requires_verified_complex_edit_and_deduplicates(tmp_path):
    store = SkillStore(tmp_path / ".hh" / "skills")
    learner = SkillLearner(store, minimum_tool_steps=5)
    run = SimpleNamespace(status="completed", tool_steps=5)
    actions = [
        {"tool": "read_file", "args": {"path": "app.py"}, "status": "ok"},
        {"tool": "search", "args": {"path": ".", "pattern": "parse"}, "status": "ok"},
        {
            "tool": "patch_file",
            "args": {"path": "app.py", "old_text": "secret old body", "new_text": "secret new body"},
            "status": "ok",
        },
        {"tool": "run_shell", "args": {"command": "pytest -q"}, "status": "ok"},
        {"tool": "read_file", "args": {"path": "tests/test_app.py"}, "status": "ok"},
    ]

    created = learner.consider("修复解析并运行测试", "已完成", run, {"actions": actions})
    assert created["status"] == "created"
    text = store.read(created["name"])
    assert "pytest -q" in text
    assert "secret old body" not in text and "secret new body" not in text

    duplicate = learner.consider("再次修复解析", "已完成", run, {"actions": actions})
    assert duplicate["status"] == "duplicate"

    no_validation = [action for action in actions if action["tool"] != "run_shell"]
    skipped = learner.consider(
        "只修改不验证",
        "完成",
        SimpleNamespace(status="completed", tool_steps=5),
        {"actions": [*no_validation, no_validation[0]]},
    )
    assert skipped == {"status": "skipped", "reason": "no_verified_reusable_edit_workflow"}


def test_runtime_skill_tools_manage_persistent_procedures(tmp_path):
    agent = make_agent(tmp_path)
    assert {"skills_list", "skill_view", "skill_manage"} <= set(agent.tools)

    created = agent.execute_tool(
        "skill_manage",
        {
            "action": "create",
            "name": "test-workflow",
            "description": "Run the project tests",
            "content": "# Test workflow\n\n1. Run pytest.",
        },
    )
    assert created.metadata["tool_status"] == "ok"
    assert "test-workflow" in agent.execute_tool("skills_list", {}).content
