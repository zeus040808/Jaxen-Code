"""Small observable event stream between AgentLoop and terminal UIs."""

from __future__ import annotations


class NullEventSink:
    def emit(self, event_type, **payload):
        del event_type, payload


class CallbackEventSink:
    def __init__(self, callback):
        self.callback = callback

    def emit(self, event_type, **payload):
        self.callback({"type": event_type, **payload})


class UsageForwardingSink:
    """子 agent 用：只把 token 用量转给父 agent 的事件出口，工具卡片和回答不转发；同时累计总量，写进子 agent 的信封。"""

    def __init__(self, parent):
        self.parent = parent
        self.totals = {"input_tokens": 0, "output_tokens": 0, "requests": 0}

    def emit(self, event_type, **payload):
        if event_type == "usage":
            for key in self.totals:
                self.totals[key] += int(payload.get(key, 0) or 0)
            self.parent.emit(event_type, **payload)
