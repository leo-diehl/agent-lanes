# agent-lanes Pool Dispatchers

This folder contains consumers for the shared queue.

## Bash wrappers

Run one wrapper per vendor when you want a headless CLI dispatcher:

```bash
bash claude.sh
bash codex.sh
```

The wrappers set `VENDOR`, `CONFIG`, and `QUEUE_ROOT`, then execute the bundled
vendor-routed dispatcher template. Each claimed task spawns a fresh headless
agent with model and effort resolved from task metadata.

## Polling chat prompt

For subscription-based chat usage, open a fresh Claude Code or Codex chat, paste
`POLLING-CHAT-PROMPT.md`, fill in the vendor identity, and send it.

The chat acts as the dispatcher: it polls the same queue, claims matching tasks,
spawns a sub-agent for the actual work, and responds on the sub-agent's behalf.

## Mixing modes

Bash wrappers and polling chats can run at the same time. They all consume the
same queue and follow the same vendor-routing metadata:

- `required_vendor`
- `model_class`
- `effort`
