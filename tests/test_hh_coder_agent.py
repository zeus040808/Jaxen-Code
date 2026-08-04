import sys
import time
from datetime import datetime

from hh_coder_agent.agent import HHCoderAgent
from hh_coder_agent.cron import cron_matches, validate_cron
from hh_coder_agent.models import ScriptedModel


def test_agent_reads_file_and_writes_auditable_artifacts(tmp_path):
    (tmp_path / "README.md").write_text("hello agent", encoding="utf-8")
    model = ScriptedModel([
        '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
        "<final>读到了 README。</final>",
    ])
    agent = HHCoderAgent(model, root=tmp_path, approval="never")

    answer = agent.ask("看看 README")

    assert "README" in answer
    run_id = agent.last_run_state.run_id
    assert (tmp_path / ".hh_coder" / "runs" / run_id / "task_state.json").exists()
    assert (tmp_path / ".hh_coder" / "runs" / run_id / "trace.jsonl").exists()
    assert (tmp_path / ".hh_coder" / "runs" / run_id / "report.json").exists()


def test_xml_write_tool_and_sandbox(tmp_path):
    model = ScriptedModel([
        '<tool name="write_file" path="notes.txt"><content>ok</content></tool>',
        "<final>完成。</final>",
    ])
    agent = HHCoderAgent(model, root=tmp_path, approval="auto")

    assert "完成" in agent.ask("写文件")
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "ok"
    result = agent.execute_tool("read_file", {"path": "../escape.txt"})
    assert result.status == "error"
    assert "escapes workspace" in result.content


def test_project_tasks_flow(tmp_path):
    agent = HHCoderAgent(ScriptedModel(["<final>ok</final>"]), root=tmp_path, approval="never")
    a = agent.tasks.create("A")
    b = agent.tasks.create("B", blocked_by=[a.id])

    assert "blocked" in agent.tasks.claim(b.id)
    assert "claimed" in agent.tasks.claim(a.id)
    assert "completed" in agent.tasks.complete(a.id)
    assert "claimed" in agent.tasks.claim(b.id)


def test_background_notification(tmp_path):
    agent = HHCoderAgent(ScriptedModel(["<final>ok</final>"]), root=tmp_path, approval="auto")
    result = agent.execute_tool(
        "run_shell",
        {"command": f'"{sys.executable}" -c "print(\'done\')"', "background": True},
    )

    assert "background task" in result.content
    for _ in range(50):
        notes = agent.background.collect()
        if notes:
            break
        time.sleep(0.02)
    assert notes
    assert "done" in notes[0]


def test_cron_matching_and_scheduler(tmp_path):
    assert validate_cron("*/5 * * * *") is None
    assert validate_cron("*/0 * * * *")
    assert cron_matches("30 9 * * 2", datetime(2026, 7, 7, 9, 30))

    agent = HHCoderAgent(ScriptedModel(["<final>ok</final>"]), root=tmp_path, approval="never")
    job = agent.cron.schedule("30 9 * * *", "standup")
    assert not isinstance(job, str)
    fired = agent.cron.poll(datetime(2026, 7, 7, 9, 30))
    assert [item.prompt for item in fired] == ["standup"]
    assert agent.cron.consume()[0].prompt == "standup"


def test_malformed_tool_retries_then_final(tmp_path):
    model = ScriptedModel([
        "<tool>{bad json}</tool>",
        "<final>恢复完成</final>",
    ])
    agent = HHCoderAgent(model, root=tmp_path, approval="never")
    assert "恢复完成" in agent.ask("测试恢复")
