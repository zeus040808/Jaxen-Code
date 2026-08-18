"""Deprecated import bridge for the old memory module.

The runtime implementation moved to :mod:`hh_agent_pro.memory` so all memory
storage boundaries live in one package. New code should import WorkingMemory,
CuratedMemoryStore, or SessionArchive directly from there.
"""

from ..memory import WorkingMemory, default_working_state, normalize_working_state

LayeredMemory = WorkingMemory
default_memory_state = default_working_state
normalize_memory_state = normalize_working_state

__all__ = [
    "LayeredMemory",
    "WorkingMemory",
    "default_memory_state",
    "default_working_state",
    "normalize_memory_state",
    "normalize_working_state",
]
