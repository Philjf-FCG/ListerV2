import sys
import os
from pathlib import Path

# Get the directory of the conftest file itself (backend/)
current_dir = Path(__file__).parent
# Determine the root path based on the workspace structure relative to backend/
# We assume the project root is two levels up from 'backend' if tests are run from there, 
# but since we don't know the exact calling context of pytest, we will robustly find the parent
# that contains all primary components. Given the structure:
# c:\DevProjects\lister2\backend\conftest.py
# The workspace root is one level up from 'backend'.

PROJECT_ROOT = current_dir.parent.parent 

def pytest_configure(config):
    """
    Adds the project root and key package directories to sys.path to ensure 
    pytest can find internal modules (like 'app' or 'routers') when running tests.
    """
    # Add the absolute path of the project root first
    sys.path.insert(0, str(PROJECT_ROOT))

    # Also add backend itself, which is sometimes required for module imports within the package structure
    sys.path.insert(0, str(current_dir))
    
    print("\\n[Conftest] Added project root and backend directory to sys.path for pytest: " + str(PROJECT_ROOT) + ", " + str(current_dir))

# Optionally, if specific relative imports cause issues, 
# defining a general path fixture can help, but modifying sys.path in pytest_configure 
# is the standard pattern for this type of discovery failure.