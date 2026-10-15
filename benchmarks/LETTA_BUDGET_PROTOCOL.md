# Native Letta with capped model-visible tool output

Declared after observing successful native filesystem source reads with a
different output budget. Preserve the original native arm. Run all the same
24 predeclared controller questions, unchanged corpus/model, eight native turns,
Read/Grep/Glob only, with a separate loopback endpoint that caps visible output.
No new aliases, expected evidence or question-specific instructions.

At each model request, reconstruct distinct tool results from its full history.
Grep/Glob get six operations, at most 1,200 bytes per result and 4,000 total.
Only the first three Read results disclose source text, at most 1,200 bytes each.
Clip unchanged UTF-8 text with an omission marker; excess attempts return a
control-only exhaustion notice. Record the actual visible result by tool-call ID
and score that view, not the larger original CLI transcript. Keep native tool
attempts and original output volume as separate costs.

These caps concern model-visible source/search output. Native tools may read a
larger file internally, just as the core reader may hash a whole source. They
cannot retroactively prevent the native tool from executing an excess attempt.
Control-only error notices do not count as source bytes. Canonical source text
returned by live grep is evidence; glob filenames are not. This differs from
the core contract that cached index snippets require a validated fetch. Report
this as a native filesystem workflow with matched output caps, not an identical
retrieval interface or proof of production scope/version protection.

Letta and the shared controller have different prompts/tool interfaces. Any
remaining performance difference is a framework+tools+workflow result, not a
causal claim about one memory mechanism. No production defaults change from a
small diagnostic, and no budget is increased for a losing method.
