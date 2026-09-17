import pytest
from unittest.mock import MagicMock, patch

# Import modules under test - using correct paths for this project
from app.routers.sync import import_vinted_item

# --- Test Cases ---

def test_import_vinted_item_success_happy_path():
    """Tests the successful execution path: Photo download -> Copy generation -> DB commit."""
    
    # Mock the database connection/transaction context manager
    with patch('app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Mock successful session call for robustness (if applicable)
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": 123,
            "url": "https://www.vinted.com/items/123",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }
        mock_conn.execute.return_value.lastrowid = 999
        
        # Mock the httpx and generate_listing_copy functions
        with patch('app.routers.sync.httpx') as mock_httpx, \
             patch('app.routers.sync.generate_listing_copy', return_value={"title": "Test Title", "description": "Test Desc", "condition": "Good", "tags": "test"}):
            
            mock_httpx.get.return_value.json.return_value = {"title": "Success Test", "description": "Good desc"}
            
            # 1. Execute the function under test
            result = import_vinted_item(123)

            # 2. Assertions for transactional success
            assert result["status"] == "imported"
            assert result["listing_id"] == 999
            
            # Check if connection methods were called successfully (transaction commitment)
            mock_conn.commit.assert_called_once()
            
            # Verify core steps happened
            mock_httpx.get.assert_called()

def test_import_vinted_item_failure_rollback():
    """Tests that the transaction rolls back if copy generation fails."""
    
    # Mock the database connection/transaction context manager
    with patch('app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Mock successful session call for robustness (if applicable)
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": 456,
            "url": "https://www.vinted.com/items/456",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }
        
        # Mock the generate_listing_copy function to fail
        with patch('app.routers.sync.httpx') as mock_httpx, \
             patch('app.routers.sync.generate_listing_copy', side_effect=Exception("Ollama API Timeout")):
            
            mock_httpx.get.return_value.json.return_value = {"title": "Success Test", "description": "Good desc"}
            
            # 1. Execute the function under test - expect exception
            with pytest.raises(Exception, match="Ollama generation failed"):
                result = import_vinted_item(456)
            
            # Crucial: Check that rollback was called and commit was NOT called
            mock_conn.rollback.assert_called_once()
            mock_conn.commit.assert_not_called()

def test_import_vinted_item_no_local_photos():
    """Tests the scenario where no local photos are found, ensuring graceful fallback."""
    
    # Mock the database connection/transaction context manager
    with patch('app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Mock a successful HTTP call, but only for metadata retrieval
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": 789,
            "url": "https://www.vinted.com/items/789",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }
        mock_conn.execute.return_value.lastrowid = 1000

        # Mock the httpx and generate_listing_copy functions
        with patch('app.routers.sync.httpx') as mock_httpx, \
             patch('app.routers.sync.generate_listing_copy', return_value={"title": "Test Title", "description": "Test Desc", "condition": "Good", "tags": "test"}):
            
            mock_httpx.get.return_value.json.return_value = {"title": "No Photo Test", "description": "Desc"}
            
            result = import_vinted_item(789)

            # Assertions
            assert result["status"] == "imported"
            mock_conn.commit.assert_called_once()

def test_ollama_client_dependency():
    """Tests that the module correctly handles missing dependencies for AI copy generation."""
    # Mock a minimal DB connection to prevent dependency failure in the outer scope
    with patch('app.routers.sync.get_connection', return_value=MagicMock()) as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": 101,
            "url": "https://www.vinted.com/items/101",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }

        # Mock the generate_listing_copy function to fail
        with patch('app.routers.sync.httpx') as mock_httpx, \
             patch('app.routers.sync.generate_listing_copy', side_effect=ImportError("ollama_client not found")):
            
            mock_httpx.get.return_value.json.return_value = {"title": "Success Test", "description": "Good desc"}
            
            # Expect failure state and no commit
            with pytest.raises(Exception, match="Ollama generation failed"):
                result = import_vinted_item(101)