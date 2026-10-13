"""
src/agent_common/__init__.py
"""

from .ipc import (
    IPCChannel,
    IPCLogHandler,
    record_to_dict,
    dict_to_record,
    setup_child_logging,
)

__all__ = [
    "IPCChannel",
    "IPCLogHandler",
    "record_to_dict",
    "dict_to_record",
    "setup_child_logging",
]
