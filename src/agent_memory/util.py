from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Tuple


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: str) -> str:
    """Normalize search text without changing the canonical source text."""
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def stable_document_id(collection: str, real_path: Path) -> str:
    material = (collection + "\0" + str(real_path)).encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """Return a tokenizer-independent conservative upper-bound unit count.

    A UTF-8 byte ceiling deliberately under-fills normal BPE token budgets but
    never assumes English prose compression or a particular model tokenizer.
    """
    return max(1, len(text.encode("utf-8", "surrogatepass")))


def truncate_to_token_budget(text: str, budget: int, marker: str = "\n[…truncated…]") -> Tuple[str, bool]:
    if budget <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= budget:
        return text, False
    marker_cost = estimate_tokens(marker)
    if marker_cost > budget:
        low, high = 0, len(marker)
        while low < high:
            mid = (low + high + 1) // 2
            if estimate_tokens(marker[:mid]) <= budget:
                low = mid
            else:
                high = mid - 1
        return marker[:low], True
    target = max(0, budget - marker_cost)
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= target:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + marker, True


def split_by_token_budget(text: str, budget: int) -> list[str]:
    """Split text into conservative token-bounded windows without loss."""
    if budget <= 0:
        return []
    remaining = text
    parts = []
    while remaining:
        if estimate_tokens(remaining) <= budget:
            parts.append(remaining)
            break
        low, high = 1, len(remaining)
        while low < high:
            mid = (low + high + 1) // 2
            if estimate_tokens(remaining[:mid]) <= budget:
                low = mid
            else:
                high = mid - 1
        cut = low
        newline = remaining.rfind("\n", max(0, cut - 400), cut)
        if newline > 0:
            cut = newline + 1
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    return parts


def slugify(value: str, fallback: str = "memory") -> str:
    value = normalize_text(value).strip().casefold()
    value = re.sub(r"[^\w\-가-힣]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return (value or fallback)[:80]


def json_dumps(value: Any, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(temporary), flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        os.chmod(str(path), mode)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_create_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically create a new file without ever replacing an existing name."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(temporary), flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(str(temporary), str(path))
        os.chmod(str(path), mode)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
