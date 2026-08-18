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
