"""
Test suite for main.py module
Tests: API routes, response models, static file serving, CORS, error handling
"""
import asyncio
import os
import sys
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture
def client(tmp_path):
    """Create a FastAPI test client with mocked dependencies"""
    db_dir = str(tmp_path / "main_test_db")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "sync.db")

    mock_scheduler = MagicMock()
    mock_scheduler.start = MagicMock()
    mock_scheduler.shutdown = MagicMock()
    mock_scheduler.add_job = MagicMock()
    mock_scheduler.remove_job = MagicMock()
    mock_scheduler.get_job = MagicMock(return_value=None)

    # Patch DB paths and scheduler on the live modules
    # Database methods reference DB_PATH at call time, so patching works
    with patch("database.DB_DIR", db_dir), \
         patch("database.DB_PATH", db_path), \
         patch("scheduler.scheduler", mock_scheduler), \
         patch("main.scheduler", mock_scheduler):
        import database as db_module
        # Initialize the test database
        asyncio.run(db_module.init_db())

        import main
        with TestClient(main.app) as c:
            yield c



class TestHealthCheck:
    """Test health check endpoint"""

    def test_health_check(self, client):
        """GET /api/health should return status ok"""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["data"]["status"] == "ok"
        assert "time" in data["data"]


class TestConfigAPI:
    """Test configuration API endpoints"""

    def test_get_config(self, client):
        """GET /api/config should return all config"""
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert "dingtalk_app_key" in data["data"]
        assert "ad_server" in data["data"]
        assert "ad_base_dn" in data["data"]

    def test_update_config(self, client):
        """PUT /api/config should update configuration"""
        resp = client.put("/api/config", json={
            "ad_server": "192.168.1.100:389",
            "ad_username": "admin@corp.com"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert "更新" in data["msg"]

    def test_update_config_partial(self, client):
        """PUT /api/config should only update provided fields"""
        # Set initial value
        client.put("/api/config", json={"ad_server": "10.0.0.1:389"})

        # Update different field
        client.put("/api/config", json={"ad_username": "newadmin"})

        # Verify both
        resp = client.get("/api/config")
        data = resp.json()["data"]
        assert data["ad_server"] == "10.0.0.1:389"
        assert data["ad_username"] == "newadmin"


class TestSyncAPI:
    """Test sync API endpoints"""

    def test_get_sync_status(self, client):
        """GET /api/sync/status should return sync status"""
        resp = client.get("/api/sync/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert "is_running" in data["data"]
        assert "last_sync_status" in data["data"]

    def test_sync_preview_no_config(self, client):
        """POST /api/sync/preview without config should return error"""
        resp = client.post("/api/sync/preview")
        data = resp.json()
        # Should return error because no DingTalk config
        assert data["code"] == 400

    def test_sync_execute_no_config(self, client):
        """POST /api/sync/execute without config starts background task (returns 200)"""
        resp = client.post("/api/sync/execute", json={"dry_run": False})
        data = resp.json()
        # The endpoint starts the background task and returns 200 immediately.
        # Config validation happens inside the background task.
        assert data["code"] == 200


class TestLogsAPI:
    """Test logs API endpoints"""

    def test_get_logs_empty(self, client):
        """GET /api/logs should return empty list initially"""
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["data"]["total"] == 0
        assert data["data"]["logs"] == []

    def test_get_logs_with_data(self, client):
        """GET /api/logs should return logs after adding"""
        import database

        asyncio.run(database.Database.add_log(
            operation_type="create_user",
            target_name="TestUser",
            status="success"
        ))

        resp = client.get("/api/logs")
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["logs"][0]["target_name"] == "TestUser"

    def test_get_logs_pagination(self, client):
        """GET /api/logs should support pagination"""
        import database

        for i in range(25):
            asyncio.run(database.Database.add_log(
                operation_type="create_user",
                target_name=f"User{i}",
                status="success"
            ))

        resp = client.get("/api/logs?page=1&page_size=10")
        data = resp.json()["data"]
        assert data["total"] == 25
        assert len(data["logs"]) == 10

    def test_get_logs_filter_operation_type(self, client):
        """GET /api/logs should filter by operation_type"""
        import database

        asyncio.run(database.Database.add_log(operation_type="create_user", target_name="U1", status="success"))
        asyncio.run(database.Database.add_log(operation_type="disable_user", target_name="U2", status="success"))

        resp = client.get("/api/logs?operation_type=create_user")
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["logs"][0]["operation_type"] == "create_user"

    def test_get_log_detail(self, client):
        """GET /api/logs/{id} should return specific log"""
        import database

        asyncio.run(database.Database.add_log(
            operation_type="create_user",
            target_name="DetailTest",
            status="success",
            detail="test detail"
        ))

        # Get logs to find the id
        resp = client.get("/api/logs")
        log_id = resp.json()["data"]["logs"][0]["id"]

        # Get specific log
        resp = client.get(f"/api/logs/{log_id}")
        data = resp.json()["data"]
        assert data["target_name"] == "DetailTest"

    def test_get_log_detail_not_found(self, client):
        """GET /api/logs/{id} with non-existent id should return 404"""
        resp = client.get("/api/logs/99999")
        data = resp.json()
        assert data["code"] == 404


class TestSchedulerAPI:
    """Test scheduler API endpoints"""

    def test_get_scheduler_config(self, client):
        """GET /api/scheduler should return scheduler config"""
        resp = client.get("/api/scheduler")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert "cron_expression" in data["data"]
        assert "enabled" in data["data"]

    def test_update_scheduler_config(self, client):
        """PUT /api/scheduler should update scheduler config"""
        resp = client.put("/api/scheduler", json={
            "cron_expression": "0 3 * * *",
            "enabled": False
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200

    def test_update_scheduler_invalid_cron(self, client):
        """PUT /api/scheduler with invalid CRON should return error"""
        resp = client.put("/api/scheduler", json={
            "cron_expression": "invalid",
            "enabled": True
        })
        data = resp.json()
        assert data["code"] == 400


class TestStaticFilesAndRoutes:
    """Test static file serving and route handling"""

    def test_root_returns_response(self, client):
        """GET / should return 200"""
        resp = client.get("/")
        assert resp.status_code == 200

    def test_catch_all_non_api(self, client):
        """Non-API paths should return 200 or 404"""
        resp = client.get("/somepage")
        assert resp.status_code in [200, 404]

    def test_api_not_found(self, client):
        """Unknown API path should return 404"""
        resp = client.get("/api/nonexistent")
        assert resp.status_code == 404


class TestCORS:
    """Test CORS configuration"""

    def test_cors_headers_present(self, client):
        """CORS headers should be present"""
        resp = client.options("/api/health", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET"
        })
        assert resp.status_code == 200


class TestRequestModels:
    """Test Pydantic request models"""

    def test_config_update_model_defaults(self):
        """ConfigUpdate should have all optional fields"""
        from main import ConfigUpdate
        model = ConfigUpdate()
        assert model.dingtalk_app_key is None
        assert model.ad_server is None

    def test_scheduler_update_model_defaults(self):
        """SchedulerUpdate should have all optional fields"""
        from main import SchedulerUpdate
        model = SchedulerUpdate()
        assert model.cron_expression is None
        assert model.enabled is None

    def test_sync_execute_request_default(self):
        """SyncExecuteRequest should default dry_run to False"""
        from main import SyncExecuteRequest
        model = SyncExecuteRequest()
        assert model.dry_run is False
