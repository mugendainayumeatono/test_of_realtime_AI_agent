"""
src/cli.py: Compatibility entry point for the CLI tool.
Seamlessly aliases to cli/cli.py.
"""
import importlib.util
import sys
from pathlib import Path

_cli_file = Path(__file__).resolve().parent.parent / "cli" / "cli.py"
_spec = importlib.util.spec_from_file_location("cli", _cli_file)
_real_cli = importlib.util.module_from_spec(_spec)
sys.modules["cli"] = _real_cli
sys.modules["src.cli"] = _real_cli
_spec.loader.exec_module(_real_cli)

for _k, _v in _real_cli.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

def __getattr__(name):
    return getattr(_real_cli, name)

def __setattr__(name, value):
    setattr(_real_cli, name, value)

if __name__ == "__main__":
    _real_cli.main()
