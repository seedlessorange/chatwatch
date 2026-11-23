# Settings that make Claude Code use less of your plan

Paste the text between the lines into a Claude Code chat. It asks Claude to check each setting against your version before changing anything, back up your settings first, and measure the difference.

The menu bar app shows how many of these you already have, and its "Copy settings advice" button copies the same text.

<!-- advice-start -->
Please tune my Claude Code settings so it uses less of my plan. Work carefully:

1. Back up ~/.claude/settings.json next to itself with today's date in the name.
2. Find my Claude Code version, and confirm that every setting name below exists in it before using it (a wrong name is silently ignored). Skip any that don't exist and tell me.
3. Measure a fresh session's starting context first: in an empty folder, run `claude -p "Reply with OK only." --model haiku --output-format json --no-session-persistence` with ANTHROPIC_API_KEY unset, and add up the input, cache read and cache write tokens.

Then apply these to ~/.claude/settings.json, keeping everything else as it is:

- Opus at medium effort by default: under "modelSettings", set "effortLevel" to "medium" for my Opus model. If I use Fable, leave it at high, since I only use it for hard problems. I can type /effort high in any chat that needs more.
- Compact earlier: "autoCompactWindow": 250000.
- Subagents keep a 1-hour prompt cache instead of 5 minutes: "subagentPromptCacheTtl": "1h".
- Unnamed subagents (general-purpose, Explore) on Sonnet: in "env", set "CLAUDE_CODE_SUBAGENT_MODEL" to "sonnet". Do not set CLAUDE_CODE_SUBAGENT_MODEL_FORCE, so agents that name their own model keep it.
- Cap shell output: in "env", set "BASH_MAX_OUTPUT_LENGTH" to "10000".
- Skills I don't use: count which skills my chats used in the last 30 days (Skill tool calls and slash commands in ~/.claude/projects/*/*.jsonl). Under "skillOverrides", set every other skill in ~/.claude/skills to "name-only", so it stays usable by name without its description loading into every chat.

Afterwards, measure the fresh session again the same way and tell me the before and after. Then run one tiny general-purpose subagent and check in its transcript that it ran on Sonnet and wrote to the 1-hour cache. Finally, list any claude.ai connectors that load in Claude Code but aren't signed in, so I can remove them on claude.ai.
<!-- advice-end -->
