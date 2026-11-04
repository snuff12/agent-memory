"""Durable receipts for retries of one explicitly authorized capture.

Receipts contain hashes and a random card identity, never a replayable body.
An intent without an identifiable card is uncertain and must not be replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml

from .memory_policy import MAX_FRONTMATTER_BYTES, _CardLoader, parse_memory
from .util import atomic_create_text, atomic_write_text, utc_now


class CaptureConflict(ValueError):
    pass


class CaptureUncertain(ValueError):
    pass


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path):
    if path.is_symlink():
        raise ValueError("Capture receipt directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Capture receipt directory must be private (0700)")


@contextmanager
def _receipt_lock(directory):
    # Independent of the rebuildable index and its sync lock.
    if os.name != "posix":
        raise ValueError("Capture operation IDs require POSIX file locks")
    import fcntl
    descriptor = os.open(str(directory / "capture.lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Capture receipt lock must be a private regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_receipt(path, receipt, *, create=False):
    writer = atomic_create_text if create else atomic_write_text
    writer(path, json.dumps(receipt, sort_keys=True) + "\n", mode=0o600)
    _sync_directory(path.parent)


def _read_receipt(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or stat.S_IMODE(info.st_mode) & 0o077:
            raise CaptureUncertain("capture uncertain: receipt is not a private bounded regular file")
        try:
            receipt = json.load(handle)
        except (ValueError, UnicodeError):
            raise CaptureUncertain("capture uncertain: unreadable receipt; inspect without retrying a new ID") from None
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("state") not in {"prepared", "completed", "uncertain"}
            or any(not isinstance(receipt.get(key), str) or not re.fullmatch(pattern, receipt[key])
                   for key, pattern in (("fingerprint", r"[a-f0-9]{64}"),
                                        ("identity", r"[a-f0-9]{32}"), ("content_sha256", r"[a-f0-9]{64}")))):
        raise CaptureUncertain("capture uncertain: invalid receipt; inspect without recapture")
    return receipt


def _find_card(vault, identity):
    matches = []
    def failed(error):
        raise error
    for root, directories, files in os.walk(vault, followlinks=False, onerror=failed):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() != ".md" or path.is_symlink():
                continue
            if not path.is_file():
                raise CaptureUncertain("capture uncertain: non-regular vault card; inspect without recapture")
            with path.open("r", encoding="utf-8") as handle:
                header = handle.read(MAX_FRONTMATTER_BYTES)
            end = header.find("\n---\n", 4)
            if header.startswith("---\n") and end >= 0 and "capture_operation" in header[4:end]:
                try:
                    metadata = yaml.load(header[4:end], Loader=_CardLoader)
                except (yaml.YAMLError, ValueError, RecursionError):
                    continue
                if isinstance(metadata, dict) and metadata.get("capture_operation") == identity:
                    matches.append(path)
    return matches


def capture_with_identity(home, operation_id, inputs, render):
    """Called under the normal sync lock; returns only a current card path."""
    if (not isinstance(operation_id, str) or not operation_id.strip()
            or len(operation_id.encode("utf-8")) > 256
            or any(unicodedata.category(c) == "Cc" for c in operation_id)):
        raise ValueError("capture operation ID must be 1–256 UTF-8 bytes without control characters")
    directory = home / "capture-operations"
    _private_directory(directory)
    _sync_directory(home)
    receipt_path = directory / (_digest(operation_id) + ".json")
    fingerprint = _digest(json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    with _receipt_lock(directory):
        if receipt_path.exists() or receipt_path.is_symlink():
            receipt = _read_receipt(receipt_path)
            if receipt["fingerprint"] != fingerprint:
                raise CaptureConflict("capture conflict: operation ID was used with different inputs")
            matches = _find_card(home / "vault", receipt["identity"])
            if len(matches) != 1:
                receipt["state"] = "uncertain"
                _write_receipt(receipt_path, receipt)
                raise CaptureUncertain("capture uncertain or unavailable: expected one current card, found %d; no card created" % len(matches))
            candidate = matches[0]
            text = candidate.read_text(encoding="utf-8")
            if parse_memory(candidate, text)["error"]:
                raise CaptureUncertain("capture uncertain: identified card metadata is invalid; no card created")
            if receipt["state"] != "completed" and _digest(text) != receipt["content_sha256"]:
                raise CaptureUncertain("capture uncertain: incomplete operation's card was modified; no card created")
            _sync_directory(candidate.parent)
            receipt["state"] = "completed"
            _write_receipt(receipt_path, receipt)
            return candidate
        identity = uuid.uuid4().hex
        created = utc_now()
        content = render(created, identity)
        candidate = home / "vault" / "inbox" / (created[:10] + "-" + identity + ".md")
        receipt = {"version": 1, "state": "prepared", "fingerprint": fingerprint,
                   "identity": identity, "created_at": created, "content_sha256": _digest(content)}
        # Both names must be durable before a completed receipt is committed.
        _write_receipt(receipt_path, receipt, create=True)
        atomic_create_text(candidate, content, mode=0o600)
        _sync_directory(candidate.parent)
        _sync_directory(candidate.parent.parent)
        receipt["state"] = "completed"
        _write_receipt(receipt_path, receipt)
        return candidate
