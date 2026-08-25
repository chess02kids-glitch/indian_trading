from unittest.mock import MagicMock, patch

from store.realtime import get_realtime_client, heartbeat


def test_realtime_client_subscriptions():
    with patch("store.realtime.get_supabase_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        client = get_realtime_client()

        # Subscribe to health_state
        client.subscribe("health_state", "INSERT", lambda x: None)
        assert "public:health_state" in client.subscriptions

        # Start listening
        client.start_listening()
        mock_client.realtime.channel.return_value.subscribe.assert_called()

        # Unsubscribe
        client.unsubscribe_all()
        mock_client.realtime.channel.return_value.unsubscribe.assert_called()
        assert len(client.subscriptions) == 0


def test_heartbeat_upsert():
    with patch("store.realtime.get_supabase_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        heartbeat("test_service", "HEALTHY", {"cpu": 10})
        mock_client.table.assert_called_with("health_state")
        mock_client.table.return_value.upsert.assert_called()
