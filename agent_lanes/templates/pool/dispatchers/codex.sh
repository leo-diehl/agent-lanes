#!/usr/bin/env bash
exec env \
  VENDOR=codex \
  QUEUE_ROOT={{STORE_PATH}} \
  CONFIG={{CONFIG_PATH}} \
  bash {{DISPATCHER_TEMPLATE}} "$@"
