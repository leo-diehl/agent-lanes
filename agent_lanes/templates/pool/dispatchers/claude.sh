#!/usr/bin/env bash
exec env \
  VENDOR=claude \
  QUEUE_ROOT={{STORE_PATH}} \
  CONFIG={{CONFIG_PATH}} \
  bash {{DISPATCHER_TEMPLATE}} "$@"
