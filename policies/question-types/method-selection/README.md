# Method-selection policy adapter

TaskFoundry does not own a second method-selection rule set.

Before each numbered `outline` or `author` stage, resolve the project Skill Bank at
`/personal/codex-workspace/question-from-questions/.skillbank` with:

- `role=teacher`
- `task_type=method-selection`
- `stage=outline|author`
- `profile=multi-question-input`

The resolved complete Execution Skill, Experience Bank snapshot, and Experience
Cards are the only runtime knowledge for that stage. The source rules are owned by
`/personal/codex-workspace/question-from-questions` and are verified by the
Execution Skill's `RULE_SOURCES.json`.
