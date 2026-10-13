"""Initialize a private memory home from distributable, synthetic resources."""
from __future__ import annotations

import json
import stat
from importlib import resources
from pathlib import Path
from typing import Optional

from .config import default_home
from .util import atomic_create_text


def initialize(home: Optional[Path] = None, *, demo: bool = False) -> Path:
    requested = (home or default_home()).expanduser()
    if requested.is_symlink():
        raise ValueError("Memory home must not be a symlink")
    destination = requested.resolve(strict=False)
    if destination.exists():
        if not destination.is_dir():
            raise NotADirectoryError("Memory home must be a directory")
        if any(destination.iterdir()):
            raise FileExistsError("Memory home is not empty; choose a new directory. Existing data was not changed.")
        if stat.S_IMODE(destination.stat().st_mode) & 0o077:
            raise ValueError("Memory home must be private; use chmod 700 on this empty directory")
    else:
        destination.mkdir(mode=0o700, parents=True)

    for name in ("knowledge", "feedback", "eval", "vault", "vault/inbox", "vault/semantic",
                 "vault/episodic", "vault/procedural", "vault/decisions", "vault/wiki"):
        (destination / name).mkdir(mode=0o700)

    bundle = resources.files("agent_memory").joinpath("resources")
    if demo:
        for name in ("retrieval.md", "learning.md"):
            atomic_create_text(destination / "knowledge" / name,
                               bundle.joinpath("demo", name).read_text(encoding="utf-8"), mode=0o600)
        atomic_create_text(destination / "eval" / "gold_queries.jsonl",
                           bundle.joinpath("demo", "gold_queries.jsonl").read_text(encoding="utf-8"), mode=0o600)
    configuration = json.loads(bundle.joinpath("config.example.json").read_text(encoding="utf-8"))
    atomic_create_text(destination / "config.json", json.dumps(configuration, indent=2) + "\n", mode=0o600)
    return destination
