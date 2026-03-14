"""Wrapper entry for symbol cache index refresh tool."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_TOOL = PROJECT_ROOT / "tools" / "refresh_symbol_cache_index_names.py"


def main() -> int:
    if not ROOT_TOOL.exists():
        print(f"refresh_symbol_cache_index_names tool not found: {ROOT_TOOL}")
        return 1
    namespace = {
        "__file__": str(ROOT_TOOL),
        "__name__": "__refresh_symbol_cache_index_names_root__",
        "__package__": None,
    }
    exec(compile(ROOT_TOOL.read_text(encoding="utf-8"), str(ROOT_TOOL), "exec"), namespace)
    entry = namespace.get("main")
    if callable(entry):
        return int(entry())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
