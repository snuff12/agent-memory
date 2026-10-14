#!/usr/bin/env python3
"""Bounded publication check; no file contents or potential secrets are printed."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


PRIVATE_ROOTS = {".state", ".amem", "vault", "knowledge", "feedback", "runtime", "implementation", "experiments", "eval"}
BUILD_ROOTS = {".git", ".venv", "venv", "build", "dist"}
PATH_PATTERNS = tuple(re.compile(rb"/" + name + rb"/[^/\s`\"']+/") for name in (b"Users", b"home"))
KEY_PATTERN = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def candidates(root: Path):
    git = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=root, capture_output=True, check=False)
    if git.returncode == 0:
        return [root / item.decode("utf-8") for item in git.stdout.split(b"\0") if item]
    return [path for path in root.rglob("*") if path.is_file() and
            not any(part in BUILD_ROOTS or part == "__pycache__" or part.endswith(".egg-info")
                    for part in path.relative_to(root).parts)]


def inspect(root: Path):
    failures, count = [], 0
    for path in candidates(root):
        relative = path.relative_to(root)
        count += 1
        parts = relative.parts
        if (parts[0] in PRIVATE_ROOTS | BUILD_ROOTS or relative.as_posix() == "config.json"
                or parts[:2] in {("benchmarks", "data"), ("benchmarks", "runs")}):
            failures.append((str(relative), "private runtime data"))
            continue
        if path.is_symlink():
            failures.append((str(relative), "symlink in public source"))
            continue
        if not path.is_file():
            failures.append((str(relative), "missing source file"))
            continue
        if path.stat().st_size > 1_000_000 or ".sqlite3" in path.name or path.suffix in {".db", ".npz", ".onnx", ".key", ".pem", ".log"} or path.name.startswith(".env"):
            failures.append((str(relative), "runtime, credential, or oversized file"))
            continue
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in PATH_PATTERNS):
            failures.append((str(relative), "device-specific home path"))
        if KEY_PATTERN.search(payload):
            failures.append((str(relative), "private key marker"))
    return count, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    count, failures = inspect(args.root.resolve())
    for path, reason in failures:
        print("FAIL: %s (%s)" % (path, reason))
    if not failures:
        print("PASS: %d public source files; no runtime payload, device-specific home path, or private key marker" % count)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
