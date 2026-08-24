import pytest
from unittest.mock import patch, MagicMock

from models.repositories import UsersRepository
from portfolio.repositories import OrdersRepository
from research.repositories import ExperimentsRepository


@patch("models.repositories.get_supabase_client")
def test_users_repository_create(mock_get_client):
    mock_client = MagicMock()
    mock_client.table().insert().execute.return_value = MagicMock(data=[{"id": "user-123", "email": "test@test.com"}])
    mock_get_client.return_value = mock_client
    
    repo = UsersRepository()
    result = repo.create("test@test.com", "trader")
    
    assert result["id"] == "user-123"
    assert result["email"] == "test@test.com"


@patch("portfolio.repositories.get_supabase_client")
def test_orders_repository_create(mock_get_client):
    mock_client = MagicMock()
    mock_client.table().insert().execute.return_value = MagicMock(data=[{"id": "order-123"}])
    mock_get_client.return_value = mock_client
    
    repo = OrdersRepository()
    result = repo.create_order("user-123", "RELIANCE.NS", "BUY", 10.0, 100.0)
    
    assert result["id"] == "order-123"


@patch("research.repositories.get_supabase_client")
def test_experiments_repository_save(mock_get_client):
    mock_client = MagicMock()
    mock_client.table().insert().execute.return_value = MagicMock(data=[{"id": "exp-123"}])
    mock_get_client.return_value = mock_client
    
    repo = ExperimentsRepository()
    result = repo.save_experiment("hyp-01", "SUCCESS")
    
    assert result["id"] == "exp-123"
