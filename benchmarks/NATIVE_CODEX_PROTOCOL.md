# Native Codex + actual memory MCP, matched output budgets

Declared after comparing the common JSON controller and native Letta. A custom
JSON action controller is not the native Codex agent, so do not infer overall
engine quality or agent reliability from that controller's score.

Run all 24 previously frozen controller tasks, same sources, same Codex model
and reasoning. Codex uses the actual local memory MCP schemas and implementation
through a per-task benchmark-only wrapper. Disable shell, web, apps, plugins,
skills search, memory capture and subagents. The wrapper has access only to the
synthetic/public memory home. No expected evidence enters the agent or wrapper.

Enforce three candidates, six searches, 1,200 bytes per search/4,000 total, three
validated fetches of 1,200 bytes each, matching the common reader's output caps.
Keep scope, strict MCP argument validation, ref/current-source checks and literal
follow-up validation. Catalog is not exposed. Count failed/budget-exhausted
attempts separately. The CLI has a 240-second process-group watchdog per task;
it does not expose the JSON controller's exact eight-decision loop. Record native
tool counts, completion/timeout, actual usage and evidence delivered.

Use per-invocation MCP configuration, not a persistent global integration. Do
not register a production server or change the operational memory home. Fetch
text is unchanged canonical evidence. Index search snippets remain navigation,
even when they contain a plausible answer. Publish only compact checks, never
authentication, private CLI events or device-specific source paths.

Different native prompts/interfaces remain a confound. Report this as the
existing engine used through native Codex tools; do not attribute gains to an
unimplemented new search algorithm or tune prompts/cases against failures.
