# Contributing

Use a fork and a branch for changes. Describe the observable behavior changed
and how you verified it. Add a focused test when changing a retrieval, privacy,
storage, or freshness contract.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e . build
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python scripts/check_public_tree.py
.venv/bin/python -m build
```

Keep source data read only and search/fetch policy consistent. Parser or chunker
changes must invalidate affected projections. Test with temporary synthetic
documents, never personal memories, private project code, or proprietary files.

Do not commit a real `config.json`, vault, index, feedback queue, logs, credentials,
runtime, or model files. `.gitignore` protects common locations; the source check
adds a bounded verification of the files that would be published. Neither is a
general content scanner. Review any new fixture or export before adding it.

The default demo evaluates a small synthetic corpus. Larger retrieval changes
need a suitable independent corpus and evaluation cases, including negative
queries and output-budget checks.
