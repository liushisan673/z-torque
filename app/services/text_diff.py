"""轻量文本差异统计（标准库实现，不引入额外依赖）。"""

from __future__ import annotations

import difflib


def line_changes(before: str, after: str) -> tuple[int, int]:
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    additions = 0
    deletions = 0
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            deletions += old_end - old_start
        if tag in ("insert", "replace"):
            additions += new_end - new_start
    return additions, deletions
