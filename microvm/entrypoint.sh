#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Starts the lifecycle hooks server (9000) and VS Code (8080).
#
# VS Code runs without its own connection token: the only way to reach port 8080
# is through the MicroVM proxy, which requires a MicroVM auth token that only
# CloudFront holds on the user's behalf (see infra/ide_router.js).
set -euo pipefail

python3 /opt/microvm/hooks.py &

exec /opt/openvscode-server/bin/openvscode-server \
  --host 0.0.0.0 \
  --port 8080 \
  --server-base-path /ide \
  --without-connection-token \
  --default-folder /home/coder/workspace \
  --telemetry-level off
