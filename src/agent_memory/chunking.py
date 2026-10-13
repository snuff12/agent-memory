from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .util import estimate_tokens, normalize_text


HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    heading: str
    locator: str
    start_line: Optional[int]
    end_line: Optional[int]
    page: Optional[int]
    text: str
    token_estimate: int


def discover_title(text: str, fallback: str) -> str:
    for line in normalize_text(text).splitlines()[:80]:
        match = HEADING_RE.match(line)
        if match:
            return match.group(2).strip()[:240]
    return fallback


def _line_windows(
    lines: Sequence[str],
    base_line: int,
    heading: str,
    locator: str,
    page: Optional[int],
    target_chars: int,
    overlap_chars: int,
) -> List[ChunkDraft]:
    chunks: List[ChunkDraft] = []
    cursor = 0
    while cursor < len(lines):
        end = cursor
        length = 0
        while end < len(lines):
            added = len(lines[end]) + 1
            if end > cursor and length + added > target_chars:
                break
            length += added
            end += 1
            if length >= target_chars:
                break

        if end == cursor:
            end += 1

        block = "\n".join(lines[cursor:end]).strip()
        if len(block) > target_chars * 2 and end == cursor + 1:
            offset = 0
            while offset < len(block):
                piece = block[offset : offset + target_chars]
                if piece.strip():
                    chunks.append(
                        ChunkDraft(
                            ordinal=0,
                            heading=heading,
                            locator=locator,
                            start_line=base_line + cursor,
                            end_line=base_line + cursor,
                            page=page,
                            text=piece.strip(),
                            token_estimate=estimate_tokens(piece),
                        )
                    )
                offset += max(1, target_chars - overlap_chars)
        elif block:
            chunks.append(
                ChunkDraft(
                    ordinal=0,
                    heading=heading,
                    locator=locator,
                    start_line=base_line + cursor,
                    end_line=base_line + end - 1,
                    page=page,
                    text=block,
                    token_estimate=estimate_tokens(block),
                )
            )

        if end >= len(lines):
            break
        overlap = 0
        next_cursor = end
        while next_cursor > cursor + 1 and overlap < overlap_chars:
            next_cursor -= 1
            overlap += len(lines[next_cursor]) + 1
        cursor = max(cursor + 1, next_cursor)
    return chunks


def chunk_text(
    text: str,
    target_chars: int,
    overlap_chars: int,
    locator: str = "",
    page: Optional[int] = None,
    line_offset: int = 1,
    markdown_headings: bool = True,
) -> List[ChunkDraft]:
    normalized = normalize_text(text)
    lines = normalized.splitlines()
    if not lines:
        return []

    sections = []
    current_heading = ""
    current_start = 0
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line) if markdown_headings else None
        if match and index > current_start:
            sections.append((current_heading, current_start, index))
            current_heading = match.group(2).strip()
            current_start = index
        elif match:
            current_heading = match.group(2).strip()
    sections.append((current_heading, current_start, len(lines)))

    drafts: List[ChunkDraft] = []
    for heading, start, end in sections:
        section_locator = locator
        if heading:
            section_locator = (locator + " · " + heading).strip(" ·")
        drafts.extend(
            _line_windows(
                lines[start:end],
                base_line=line_offset + start,
                heading=heading,
                locator=section_locator,
                page=page,
                target_chars=target_chars,
                overlap_chars=overlap_chars,
            )
        )

    return [
        ChunkDraft(
            ordinal=index,
            heading=draft.heading,
            locator=draft.locator,
            start_line=draft.start_line,
            end_line=draft.end_line,
            page=draft.page,
            text=draft.text,
            token_estimate=draft.token_estimate,
        )
        for index, draft in enumerate(drafts)
    ]
