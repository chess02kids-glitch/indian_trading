"""Supabase Realtime subscriptions for Phase B synchronization."""

import logging
import threading
from typing import Any, Callable, Dict

from config.database import get_supabase_client

logger = logging.getLogger(__name__)


class RealtimeClient:
    """Manages Supabase realtime subscriptions for dashboard and experiment sync."""

    def __init__(self):
        self.client = get_supabase_client()
        self.subscriptions: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def subscribe(
        self, table: str, event: str, callback: Callable[[dict], None]
    ) -> None:
        """Subscribe to a specific table and event (*, INSERT, UPDATE, DELETE)."""
        with self._lock:
            channel_name = f"public:{table}"
            if channel_name not in self.subscriptions:
                # Use the underlying gotrue realtime client from supabase-py
                channel = self.client.realtime.channel(channel_name)
                self.subscriptions[channel_name] = channel
            else:
                channel = self.subscriptions[channel_name]

            def wrapped_callback(payload: dict) -> None:
                try:
                    callback(payload)
                except Exception as e:
                    logger.error(f"Error in realtime callback for {table}: {e}")

            # Register the event listener
            channel.on(
                "postgres_changes",
                event=event,
                schema="public",
                table=table,
                callback=wrapped_callback,
            )

    def start_listening(self) -> None:
        """Start listening to all configured channels."""
        with self._lock:
            for channel_name, channel in self.subscriptions.items():
                channel.subscribe()
                logger.info(f"Subscribed to realtime channel: {channel_name}")

    def unsubscribe_all(self) -> None:
        """Unsubscribe from all active channels."""
        with self._lock:
            for channel_name, channel in self.subscriptions.items():
                channel.unsubscribe()
                logger.info(f"Unsubscribed from realtime channel: {channel_name}")
            self.subscriptions.clear()


# Singleton instance
_realtime_client = None


def get_realtime_client() -> RealtimeClient:
    global _realtime_client
    if _realtime_client is None:
        _realtime_client = RealtimeClient()
    return _realtime_client


def heartbeat(service_name: str, status: str, metadata: dict | None = None) -> None:
    """Update health_state table to persist system status for realtime sync."""
    client = get_supabase_client()
    data = {"service_name": service_name, "status": status, "metadata": metadata or {}}
    try:
        client.table("health_state").upsert(data, on_conflict="service_name").execute()
    except Exception as e:
        logger.error(f"Failed to send heartbeat for {service_name}: {e}")
