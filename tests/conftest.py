"""
Shared test fixtures and configuration
"""
import sys
import os
import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend directory to path so we can import modules
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_db_dir(tmp_path):
    """Provide a temporary directory for test database"""
    return str(tmp_path)
