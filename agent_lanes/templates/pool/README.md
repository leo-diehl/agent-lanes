# agent-lanes Shared Queue

This directory is the workspace-level queue for `{{WORKSPACE_ID}}`.

The queue owns:

- `handoff.yaml` - the engine config for the shared pool.
- `state/` - task and response state for all projects using this pool.
- lane `default` - the shared subscription lane.

Projects point their local `handoff/handoff.yaml` at this queue by setting
`queue_root` to the absolute path of this `state/` directory. Dispatchers connect
to this config and store, then route tasks by metadata:

- `required_vendor`
- `model_class`
- `effort`

Inspect the queue with:

```bash
agent-lanes \
  --config <absolute-path>/handoff.yaml \
  --store <absolute-path>/state \
  status --rack --json
```

The scaffolded dispatcher wrappers live in the sibling `_dispatchers/`
directory. You can run a bash dispatcher or paste the polling chat prompt into a
chat that can call shell commands and spawn sub-agents.

For the protocol contract, see `CONTRACT.md` section 17 in the agent-lanes
checkout.
