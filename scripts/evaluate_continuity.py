#!/usr/bin/env python3
"""Create a synthetic-only home and export bounded continuity evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_memory.bootstrap import initialize
from agent_memory.cli import _evaluate
from agent_memory.config import load_config
from agent_memory.indexer import sync_all
from agent_memory.util import atomic_write_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True, help='New, empty synthetic memory home.')
    args = parser.parse_args()
    home = initialize(args.home)
    fixtures = Path(__file__).resolve().parents[1] / 'tests/fixtures/continuity'
    configuration = json.loads((home / 'config.json').read_text())
    configuration['sources'][0]['name'] = 'test'
    configuration['sources'][0]['description'] = 'Synthetic continuity fixtures only.'
    atomic_write_text(home / 'config.json', json.dumps(configuration, indent=2) + '\n')
    for path in fixtures.glob('*.md'):
        atomic_write_text(home / 'knowledge' / path.name, path.read_text(encoding='utf-8'))
    cases = home / 'eval/continuity.jsonl'
    atomic_write_text(cases, (fixtures / 'cases.jsonl').read_text(encoding='utf-8'))
    config = load_config(home)
    sync_all(config)
    report = _evaluate(config, cases, evidence_output=home / 'eval/evidence.json')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed_cases'] == report['cases'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
