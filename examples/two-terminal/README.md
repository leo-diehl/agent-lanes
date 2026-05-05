# Two-terminal demo

This is a 30-second proof that agent-lanes works end to end. `agent-a.sh`
submits a review task to a lane; `agent-b.sh` long-polls the lane, claims the
task, and responds with a fixed review. Both scripts are shell-only, but they
exercise the real submit / wait / claim / respond cycle against a file-backed
queue.

## Run

```bash
cd examples/two-terminal
chmod +x agent-a.sh agent-b.sh
bash agent-b.sh &
bash agent-a.sh
wait
```

You should see `agent-a` print the response within a couple of seconds. If the
demo stalls, check that `jq` and `agent-lanes` are on `PATH`.

## How it works

- `handoff.yaml` declares a flat engine config: workspace metadata plus one
  review lane.
- Queue state lives in `state/`; the scripts pass `--config` and `--store`
  explicitly.
- `agent-a.sh` calls `submit --lane review` and gets back a task id.
- `agent-b.sh` calls `wait --lane review` to long-poll, then claims and
  responds.
- `agent-a.sh` waits on the task id and prints the body written to
  `outputs/response.md`.

This is the bare protocol. In real use, `agent-b.sh` would be replaced with the
bundled dispatcher template or the polling chat prompt from `agent_lanes/`.
