from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

from .util import estimate_tokens, normalize_text


HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A list item marker and the spaces after it; the match ends at the item's content column.
ITEM_RE = re.compile(r" *(?:[-+*]|\d{1,9}[.)])( {1,4})(?=\S)")


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


def _headings(lines: Sequence[str], fences: bool = True) -> Iterator[Tuple[int, str]]:
    """Yield ATX headings; with fences, skip CommonMark fenced code blocks.

    A fence opens with three or more backticks or tildes indented by at most
    three spaces past its container; a backtick info string cannot contain a
    backtick.  It closes on the same character, at least as long, indented at
    most three spaces past the container and followed only by spaces or tabs.
    The container is the document or the list item whose content the fence
    starts in.  A fence in a list item also ends with the item, at the first
    non-blank line indented less than the item's content; otherwise an
    unclosed fence runs to the end of the text.  Lazy continuation lines and
    other containers, such as block quotes, are approximated.
    """
    fence, base, item = "", 0, 0
    for index, line in enumerate(lines):
        if fences:
            indent = len(line) - len(line.lstrip(" "))
            if fence:
                if not line.strip() or indent >= base:
                    marker = FENCE_RE.match(line[base:])
                    if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip(" \t"):
                        fence = ""
                    continue
                fence = ""  # The list item ended, and its fence with it; read this line as usual.
            bullet = ITEM_RE.match(line)
            if bullet and indent <= item + 3:
                item = bullet.end()
            elif line.strip() and indent < item:
                item = 0
            marker = FENCE_RE.match(line[item:])
            if marker and not (marker.group(1)[0] == "`" and "`" in marker.group(2)):
                fence, base = marker.group(1), item
                continue
        match = HEADING_RE.match(line)
        if match:
            yield index, match.group(2).strip()


def discover_title(text: str, fallback: str, fences: bool = False) -> str:
    for _index, heading in _headings(normalize_text(text).splitlines()[:80], fences):
        return heading[:240]
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
    for index, heading in _headings(lines) if markdown_headings else ():
        if index > current_start:
            sections.append((current_heading, current_start, index))
            current_start = index
        current_heading = heading
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
