import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

# Import modules under test - using correct paths for this project
from app.routers.sync import (
    get_local_vinted_photos,
    relink_all_vinted_photos,
    upsert_vinted_items,
    list_vinted_items,
    import_vinted_item
)
from app.db import get_connection
from app.schemas import VintedItemIn, VintedItemOut

# --- Fixtures and Mocks Setup ---

@pytest.fixture(scope="module")
def mock_db_connection():
    """Mock the database connection context manager."""
    mock_conn = MagicMock()
    mock_transaction = MagicMock()
    # Simulate 'with get_connection() as conn:' block
    mock_transaction.__enter__.return_value = mock_conn
    mock_transaction.__exit__.return_value = None
    return mock_transaction

@pytest.fixture(autouse=True)
def mock_services():
    """Mock external services that are costly or stateful (e.g., httpx calls, Ollama)."""
    with patch('app.routers.sync.httpx', MagicMock()) as mock_httpx, \
         patch('app.routers.sync.generate_listing_copy', return_value={"title": "Test Title", "description": "Test Desc", "condition": "Good", "tags": "test"}):
        yield {
            "httpx": mock_httpx,
            "mock_generator": generate_listing_copy
        }

@pytest.fixture(scope="function")
def mock_file_system():
    """Mock filesystem operations like Path().glob."""
    with patch('pathlib.Path', MagicMock(spec=Path)) as MockPath:
        # Setup basic path mocking for local file discovery simulations
        mock_instance = MagicMock(spec=Path)
        MockPath.return_value = mock_instance
        mock_instance.glob.return_value = [] # Default to no files found

        yield mock_instance

# --- Test Cases ---

def test_import_vinted_item_success_happy_path(mock_services, mock_file_system):
    """Tests the successful execution path: Photo download -> Copy generation -> DB commit."""
    
    # Setup mocks for success scenario
    test_item_id = 123
    
    # Mock the database connection/transaction context manager
    with patch('app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Mock successful session call for robustness (if applicable)
        mock_services["httpx"].get.return_value.json.return_value = {"title": "Success Test", "description": "Good desc"}
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": test_item_id,
            "url": "https://www.vinted.com/items/123",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }
        mock_conn.execute.return_value.lastrowid = 999
        
        # 1. Execute the function under test
        result = import_vinted_item(test_item_id)

        # 2. Assertions for transactional success
        assert result["status"] == "imported"
        assert result["listing_id"] == 999
        
        # Check if connection methods were called successfully (transaction commitment)
        mock_conn.commit.assert_called_once()
        
        # Verify core steps happened
        mock_services["httpx"].get.assert_called()
        mock_services["mock_generator"].assert_called()

def test_import_vinted_item_failure_rollback(mock_services, mock_file_system):
    """Tests that the transaction rolls back if copy generation fails."""
    
    # Simulate failure during content generation phase
    mock_services["mock_generator"].side_effect = Exception("Ollama API Timeout")

    with patch('app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Mock successful session call for robustness (if applicable)
        mock_services["httpx"].get.return_value.json.return_value = {"title": "Success Test", "description": "Good desc"}
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": 456,
            "url": "https://www.vinted.com/items/456",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }
        
        # 1. Execute the function under test
        with pytest.raises(Exception, match="Ollama generation failed"):
            result = import_vinted_item(456)
        
        # Crucial: Check that rollback was called and commit was NOT called
        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()

def test_import_vinted_item_no_local_photos(mock_services, mock_file_system):
    """Tests the scenario where no local photos are found, ensuring graceful fallback."""
    
    # Ensure file system search returns nothing
    mock_file_system.return_value.glob.return_value = []

    with patch('app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        
        # Mock a successful HTTP call, but only for metadata retrieval
        mock_services["httpx"].get.return_value.json.return_value = {"title": "No Photo Test", "description": "Desc"}
        mock_conn.execute.return_value.fetchone.return_value = {
            "id": 789,
            "url": "https://www.vinted.com/items/789",
            "title": "Test Item",
            "price": "45.00",
            "photo_urls": '["https://example.com/photo.jpg"]'
        }
        mock_conn.execute.return_value.lastrowid = 1000

        result = import_vinted_item(789)

        # Assertions
        assert result["status"] == "imported"
        mock_conn.commit.assert_called_once()

def test_ollama_client_dependency(mock_services):
    """Tests that the module correctly handles missing dependencies for AI copy generation."""
    # We can't mock an entire client, but we check if the function call is guarded/handled.
    with patch('app.routers.sync.generate_listing_copy', side_effect=ImportError("ollama_client not found")):
        
        # Mock a minimal DB connection to prevent dependency failure in the outer scope
        mock_conn = MagicMock()
        with patch('app.routers.sync.get_connection', return_value=MagicMock()) as mock_db:
            mock_conn.execute.return_value.fetchone.return_value = {
                "id": 101,
                "url": "https://www.vinted.com/items/101",
                "title": "Test Item",
                "price": "45.00",
                "photo_urls": '["https://example.com/photo.jpg"]'
            }

            with pytest.raises(Exception, match="Ollama generation failed"):
                result = import_vinted_item(101)

        # Expect failure state and no commit
        mock_conn.rollback.assert_called_once()

# --- Fixtures and Mocks Setup ---

@pytest.fixture(scope="module")
def mock_connection():
    """Mock the database connection context manager."""
    mock_conn = MagicMock()
    mock_transaction = MagicMock()
    # Simulate 'with get_connection() as conn:' block
    mock_transaction.__enter__.return_value = mock_conn
    mock_transaction.__exit__.return_value = None
    
    # We need a way to mock the context manager structure, 
    # but since we can't modify backend.db or app directly here, 
    # we will patch get_connection itself in tests where possible, 
    # and assume the mocked connection object 'conn' is passed/used correctly.
    return mock_conn

@pytest.fixture(autouse=True)
def mock_services():
    """Mock external services that are costly or stateful (e.g., httpx calls, Ollama)."""
    with patch('backend.app.routers.sync.httpx', MagicMock()) as mock_httpx, \
         patch('backend.app.routers.sync.generate_listing_copy', return_value="MOCK COPY"):
        yield {
            "httpx": mock_httpx,
            "mock_generator": generate_listing_copy
        }

@pytest.fixture(scope="function")
def mock_file_system():
    """Mock filesystem operations like Path().glob."""
    with patch('pathlib.Path', MagicMock(spec=Path)) as MockPath:
        # Setup basic path mocking for local file discovery simulations
        mock_instance = MagicMock(spec=Path)
        MockPath.return_value = mock_instance
        mock_instance.glob.return_value = [] # Default to no files found

        yield mock_instance


# --- Test Cases ---

def test_vinted_import_success_happy_path(mock_services, mock_file_system):
    """Tests the successful execution path: Photo download -> Copy generation -> DB commit."""
    
    # Setup mocks for success scenario
    test_item_id = "VINTED-123"
    mock_file_system.return_value.glob.return_value = [MagicMock()] # Simulate finding local photos
    
    # Mock the database connection/transaction context manager
    with patch('backend.app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn # Simulate 'as conn:' scope
        
        # Mock successful session call for robustness (if applicable)
        mock_services["httpx"].get.return_json({"title": "Success Test", "description": "Good desc"})

        # 1. Execute the function under test
        result = import_vinted_item(test_item_id, mock_conn, *mock_file_system)

        # 2. Assertions for transactional success
        assert result["status"] == "SUCCESS"
        
        # Check if connection methods were called successfully (transaction commitment)
        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called() # Crucial: rollback should not happen on success

        # Verify core steps happened
        mock_services["httpx"].get.assert_called()
        mock_services["mock_generator"].assert_called_with("Good desc", "Success Test")
        # Check if item was inserted/updated correctly (Mocking DB insert check)
        mock_conn.insert_or_update_item.assert_called_once()


def test_vinted_import_failure_rollback(mock_services, mock_file_system):
    """Tests that the transaction rolls back if copy generation fails."""
    test_item_id = "VINTED-456"
    
    # Simulate failure during content generation phase
    mock_services["mock_generator"].side_effect = Exception("Ollama API Timeout")

    with patch('backend.app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn # Simulate 'as conn:' scope
        
        # 1. Execute the function under test
        result = import_vinted_item(test_item_id, mock_conn, *mock_file_system)

        # 2. Assertions for transactional failure (Rollback must occur)
        assert result["status"] == "FAILED"
        
        # Crucial: Check that rollback was called and commit was NOT called
        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()

    """ 
    This test validates that the state machine correctly handles external failures 
    by ensuring a database rollback, maintaining data integrity.
    """


def test_vinted_item_no_local_photos(mock_services, mock_file_system):
    """Tests the scenario where no local photos are found, ensuring graceful fallback."""
    test_item_id = "VINTED-789"
    # Ensure file system search returns nothing
    mock_file_system.return_value.glob.return_value = [] 

    with patch('backend.app.routers.sync.get_connection') as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_conn
        # Mock a successful HTTP call, but only for metadata retrieval
        mock_services["httpx"].get.return_json({"title": "No Photo Test", "description": "Desc"})

        result = import_vinted_item(test_item_id, mock_conn, *mock_file_system)

        # Assertions
        assert result["status"] == "SUCCESS" 
        mock_conn.commit.assert_called_once()


def test_ollama_client_dependency(mock_services):
    """Tests that the module correctly handles missing dependencies for AI copy generation."""
    # We can't mock an entire client, but we check if the function call is guarded/handled.
    with patch('backend.app.routers.sync.generate_listing_copy', side_effect=ImportError("ollama_client not found")):
        test_item_id = "VINTED-101"
        
        # Mock a minimal DB connection to prevent dependency failure in the outer scope
        mock_conn = MagicMock() 
        with patch('backend.app.routers.sync.get_connection', return_value=MagicMock()) as mock_db:
            result = import_vinted_item(test_item_id, mock_conn, *MagicMock())

        # Expect failure state and no commit
        assert result["status"] == "FAILED"
        mock_conn.rollback.assert_called_once()