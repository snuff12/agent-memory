"""Pack whole small Python symbols and retain names on bounded large windows."""
from __future__ import annotations

import ast
from dataclasses import replace

from .chunking import ChunkDraft, chunk_text
from .util import estimate_tokens


def _bounded(drafts, target, overlap):
    bounded = []
    for chunk in drafts:
        if len(chunk.text) <= target:
            bounded.append(chunk)
            continue
        step = max(1, target - overlap)
        for start in range(0, len(chunk.text), step):
            piece = chunk.text[start:start + target]
            if piece.strip():
                bounded.append(replace(chunk, text=piece, token_estimate=estimate_tokens(piece)))
            if start + target >= len(chunk.text):
                break
    return [replace(chunk, ordinal=index) for index, chunk in enumerate(bounded)]


def chunk_python(text: str, target: int, overlap: int):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return _bounded(chunk_text(text, target, overlap, markdown_headings=False), target, overlap)
    lines = text.splitlines()
    units = []
    cursor = 1
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [item.lineno for item in node.decorator_list])
        end = node.end_lineno
        if cursor < start:
            units.append((cursor, start - 1, "module"))
        units.append((start, end, node.name))
        cursor = end + 1
    if cursor <= len(lines):
        units.append((cursor, len(lines), "module"))
    drafts = []

    def emit(group):
        if not group:
            return
        start, end = group[0][0], group[-1][1]
        label = ", ".join(unit[2] for unit in group if unit[2] != "module")[:240] or "module"
        block = "\n".join(lines[start - 1:end])
        if len(block) == target:
            # A final newline is not part of the character count. Keep this edge whole.
            drafts.append(ChunkDraft(0, label, label, start, end, None, block.strip(), estimate_tokens(block.strip())))
            return
        drafts.extend(replace(chunk, heading=label, locator=label) for chunk in chunk_text(
            block, target, overlap, line_offset=start, markdown_headings=False))

    pending = []
    for unit in units:
        start, end, _ = unit
        if len("\n".join(lines[start - 1:end])) > target:
            emit(pending)
            pending = []
            emit([unit])
        else:
            # Include chunk_text's final newline accounting to avoid splitting a packed symbol.
            if pending and len("\n".join(lines[pending[0][0] - 1:end])) + 1 > target:
                emit(pending)
                pending = []
            pending.append(unit)
    emit(pending)
    return _bounded(drafts, target, overlap)
