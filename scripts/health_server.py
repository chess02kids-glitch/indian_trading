#!/usr/bin/env python3
"""Minimal HTTP server exposing a /health endpoint for deployment probes."""

import json
import logging
import os
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil

from config.env_validator import validate_database_health
from config.logging import setup_logging
from store.realtime import get_realtime_client

logger = logging.getLogger(__name__)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            try:
                # Runs the full connectivity and RLS validation checks
                validate_database_health()

                # Check metrics
                disk = shutil.disk_usage("/")
                mem = psutil.virtual_memory()

                # Check backup freshness (e.g. latest .enc file in ./backups)
                backup_dir = Path("./backups")
                latest_backup_age = None
                if backup_dir.exists():
                    backups = list(backup_dir.glob("*.sql.enc"))
                    if backups:
                        latest_backup = max(backups, key=os.path.getmtime)
                        import time

                        latest_backup_age = time.time() - os.path.getmtime(
                            latest_backup
                        )

                # Check realtime status
                rt_client = get_realtime_client()

                payload = {
                    "status": "ok",
                    "disk_usage_percent": disk.used / disk.total * 100,
                    "memory_usage_percent": mem.percent,
                    "latest_backup_age_seconds": latest_backup_age,
                    "realtime_connection_state": rt_client.connection_state,
                }

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "unhealthy", "error": str(e)}).encode("utf-8")
                )
        else:
            self.send_response(404)
            self.end_headers()


def run_health_server(port: int = 8080) -> None:
    # The container health probe must be reachable through its exposed port.
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)  # nosec B104
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    setup_logging()
    run_health_server()
