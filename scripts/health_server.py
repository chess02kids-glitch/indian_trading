#!/usr/bin/env python3
"""Minimal HTTP server exposing a /health endpoint for deployment probes."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from config.env_validator import validate_database_health
from config.logging import setup_logging
import logging

logger = logging.getLogger(__name__)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            try:
                # Runs the full connectivity and RLS validation checks
                validate_database_health()
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "unhealthy", 
                    "error": str(e)
                }).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def run_health_server(port: int = 8080) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    setup_logging()
    run_health_server()
