#!/usr/bin/env bash
# Restart only the read-only dashboard and verify its local health endpoint.
set -euo pipefail
sudo systemctl restart quant-india.service
sleep 2
curl --fail --silent --show-error http://127.0.0.1:8080/healthz
echo
