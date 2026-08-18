"""一次 ask() 运行过程中的状态机快照。

它回答的是：这次用户请求当前进行到哪了、调了多少次工具、最后为什么停下。
这个对象会被不断写入 run_state.json，供运行中观察和运行后复盘。
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

# 当前运行状态
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"
STATUS_WAITING = "waiting_for_user"

# 停止原因
STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_STEP_LIMIT_REACHED = "step_limit_reached"
STOP_REASON_RETRY_LIMIT_REACHED = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_DELEGATE_FAILED = "delegate_failed"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"
STOP_REASON_NEEDS_USER_INPUT = "needs_user_input"
STOP_REASON_BLOCKED = "blocked"
STOP_REASON_REPEATED_FAILED_CALL = "repeated_failed_call"
STOP_REASON_INTERRUPTED = "interrupted"
STOP_REASON_OUTPUT_TRUNCATED = "output_truncated"
STOP_REASON_MODEL_PROTOCOL_ERROR = "model_protocol_error"
STOP_REASON_EPISODE_TIMEOUT = "episode_timeout"


def _timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# 这次运行当前是什么状态
# 它不是事件历史，
# 因为每次保存都会覆盖旧的 run_state.json，只保留最新状态
@dataclass
class RunState:
    run_id: str     # 标识一次实际运行
    request_id: str
    user_request: str
    status: str = STATUS_RUNNING
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""   # 最近一次申请执行的工具名
    stop_reason: str = ""
    final_answer: str = ""
    partial_answer: str = ""
    checkpoint_id: str = ""
    resume_status: str = ""
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    finished_at: str = ""

    @classmethod
    # 创建一次run
    def create(cls, request_id, user_request, run_id=""):
        if not run_id:
            run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(run_id=run_id, request_id=request_id, user_request=user_request)

    @classmethod
    def from_dict(cls, data):
        return cls(
            run_id=str(data.get("run_id", "")),
            request_id=str(data.get("request_id") or data.get("task_id", "")),
            user_request=str(data.get("user_request", "")),
            status=str(data.get("status", STATUS_RUNNING)),
            tool_steps=int(data.get("tool_steps", 0)),
            attempts=int(data.get("attempts", 0)),
            last_tool=str(data.get("last_tool", "")),
            stop_reason=str(data.get("stop_reason", "")),
            final_answer=str(data.get("final_answer", "")),
            partial_answer=str(data.get("partial_answer", "")),
            checkpoint_id=str(data.get("checkpoint_id", "")),
            resume_status=str(data.get("resume_status", "")),
            created_at=str(data.get("created_at", "")) or _timestamp(),
            updated_at=str(data.get("updated_at", "")) or _timestamp(),
            finished_at=str(data.get("finished_at", "")),
        )

    def touch(self):
        self.updated_at = _timestamp()
        return self

    # 相关方法
    def record_attempt(self):
        # attempt 统计的是“模型被调用了几轮”，不等于 tool_steps。
        self.attempts += 1
        return self.touch()

    def record_tool(self, name):
        # tool_steps 只统计真正进入执行阶段的工具调用次数。
        self.tool_steps += 1
        self.last_tool = str(name or "")
        return self.touch()

    def stop(self, stop_reason, status=STATUS_STOPPED, final_answer=""):
        # stop_reason 和 status 分开存，是为了区分“怎么停的”和“停下时是什么状态”。
        self.status = status
        self.stop_reason = stop_reason
        if final_answer != "":
            self.final_answer = final_answer
        self.finished_at = _timestamp()
        return self.touch()

    def stop_step_limit(self, final_answer=""):
        return self.stop(STOP_REASON_STEP_LIMIT_REACHED, final_answer=final_answer)

    def stop_retry_limit(self, final_answer=""):
        return self.stop(STOP_REASON_RETRY_LIMIT_REACHED, final_answer=final_answer)

    def stop_model_error(self, final_answer=""):
        return self.stop(STOP_REASON_MODEL_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def stop_interrupted(self, final_answer="Interrupted by user."):
        return self.stop(STOP_REASON_INTERRUPTED, status=STATUS_STOPPED, final_answer=final_answer)

    def stop_output_truncated(self, partial_answer="", final_answer=""):
        self.partial_answer = str(partial_answer)
        return self.stop(
            STOP_REASON_OUTPUT_TRUNCATED,
            status=STATUS_STOPPED,
            final_answer=final_answer,
        )

    def stop_model_protocol_error(self, final_answer=""):
        return self.stop(
            STOP_REASON_MODEL_PROTOCOL_ERROR,
            status=STATUS_STOPPED,
            final_answer=final_answer,
        )

    def finish_success(self, final_answer):
        self.status = STATUS_COMPLETED
        self.stop_reason = STOP_REASON_FINAL_ANSWER_RETURNED
        self.final_answer = str(final_answer)
        self.finished_at = _timestamp()
        return self.touch()

    def wait_for_user(self, final_answer):
        return self.stop(
            STOP_REASON_NEEDS_USER_INPUT,
            status=STATUS_WAITING,
            final_answer=final_answer,
        )

    def finish_blocked(self, final_answer, stop_reason=STOP_REASON_BLOCKED):
        return self.stop(stop_reason, status=STATUS_STOPPED, final_answer=final_answer)

    # 将json格式转为可读的text
    def to_dict(self):
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "partial_answer": self.partial_answer,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }
