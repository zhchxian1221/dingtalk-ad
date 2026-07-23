"""
Test suite for database.py module
Tests: init_db, config CRUD, logs, sync_status operations
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import database
from database import Database, init_db, DEFAULT_CONFIG, DB_PATH


@pytest.fixture
async def test_db(tmp_path):
    """Create a test database with isolated DB path"""
    db_dir = str(tmp_path / "test_db_dir")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "sync.db")

    # Patch the DB_PATH used by database module
    with patch.object(database, "DB_DIR", db_dir), \
         patch.object(database, "DB_PATH", db_path):
        await init_db()
        yield db_path


class TestInitDB:
    """Test database initialization"""

    @pytest.mark.asyncio
    async def test_init_creates_tables(self, tmp_path):
        """init_db should create all three tables"""
        db_dir = str(tmp_path / "init_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            assert os.path.exists(db_path)

            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                # Check sync_logs table
                cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in await cursor.fetchall()]
                assert "sync_logs" in tables
                assert "config" in tables
                assert "sync_status" in tables

    @pytest.mark.asyncio
    async def test_init_inserts_default_config(self, tmp_path):
        """init_db should insert all default config values"""
        db_dir = str(tmp_path / "config_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT key, value FROM config")
                rows = {row[0]: row[1] for row in await cursor.fetchall()}

                for key, value in DEFAULT_CONFIG.items():
                    assert key in rows
                    assert rows[key] == value

    @pytest.mark.asyncio
    async def test_init_creates_default_sync_status(self, tmp_path):
        """init_db should create default sync_status record"""
        db_dir = str(tmp_path / "status_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT * FROM sync_status WHERE id = 1")
                row = await cursor.fetchone()
                assert row is not None
                assert row[6] == 0  # is_running = 0


class TestConfigOperations:
    """Test config get/set/update operations"""

    @pytest.mark.asyncio
    async def test_get_config_existing(self, tmp_path):
        db_dir = str(tmp_path / "get_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            value = await Database.get_config("ad_base_dn")
            assert value == DEFAULT_CONFIG["ad_base_dn"]

    @pytest.mark.asyncio
    async def test_get_config_nonexistent_with_default(self, tmp_path):
        db_dir = str(tmp_path / "default_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            value = await Database.get_config("nonexistent_key", "default_val")
            assert value == "default_val"

    @pytest.mark.asyncio
    async def test_get_config_nonexistent_without_default(self, tmp_path):
        db_dir = str(tmp_path / "nodefault_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            value = await Database.get_config("nonexistent_key")
            assert value == ""

    @pytest.mark.asyncio
    async def test_set_config_new(self, tmp_path):
        db_dir = str(tmp_path / "set_new_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.set_config("custom_key", "custom_value")

            value = await Database.get_config("custom_key")
            assert value == "custom_value"

    @pytest.mark.asyncio
    async def test_set_config_update_existing(self, tmp_path):
        db_dir = str(tmp_path / "update_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.set_config("ad_server", "192.168.1.100")
            value = await Database.get_config("ad_server")
            assert value == "192.168.1.100"

            # Update again
            await Database.set_config("ad_server", "192.168.1.200")
            value = await Database.get_config("ad_server")
            assert value == "192.168.1.200"

    @pytest.mark.asyncio
    async def test_get_all_config(self, tmp_path):
        db_dir = str(tmp_path / "all_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            config = await Database.get_all_config()

            for key, value in DEFAULT_CONFIG.items():
                assert config[key] == value

    @pytest.mark.asyncio
    async def test_update_config_batch(self, tmp_path):
        db_dir = str(tmp_path / "batch_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.update_config({
                "ad_server": "10.0.0.1",
                "ad_username": "admin@test.com",
            })

            config = await Database.get_all_config()
            assert config["ad_server"] == "10.0.0.1"
            assert config["ad_username"] == "admin@test.com"


class TestLogOperations:
    """Test sync log operations"""

    @pytest.mark.asyncio
    async def test_add_log(self, tmp_path):
        db_dir = str(tmp_path / "log_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.add_log(
                operation_type="create_user",
                target_dn="CN=Test,OU=Users,DC=corp,DC=com",
                target_name="张三",
                status="success",
                detail='{"userid": "u1"}',
            )

            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT * FROM sync_logs WHERE target_name = '张三'")
                row = await cursor.fetchone()
                assert row is not None
                assert row[2] == "create_user"  # operation_type
                assert row[5] == "success"  # status

    @pytest.mark.asyncio
    async def test_get_logs_pagination(self, tmp_path):
        db_dir = str(tmp_path / "pagination_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            # Add 25 logs
            for i in range(25):
                await Database.add_log(
                    operation_type="create_user",
                    target_name=f"User{i}",
                    status="success",
                )

            # Get page 1 (page_size=10)
            result = await Database.get_logs(page=1, page_size=10)
            assert result["total"] == 25
            assert len(result["logs"]) == 10
            assert result["page"] == 1
            assert result["page_size"] == 10

            # Get page 3 (should have 5 logs)
            result = await Database.get_logs(page=3, page_size=10)
            assert len(result["logs"]) == 5

    @pytest.mark.asyncio
    async def test_get_logs_filter_by_operation_type(self, tmp_path):
        db_dir = str(tmp_path / "filter_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.add_log(operation_type="create_user", target_name="U1", status="success")
            await Database.add_log(operation_type="disable_user", target_name="U2", status="success")
            await Database.add_log(operation_type="create_user", target_name="U3", status="success")

            result = await Database.get_logs(operation_type="create_user")
            assert result["total"] == 2
            assert all(log["operation_type"] == "create_user" for log in result["logs"])

    @pytest.mark.asyncio
    async def test_get_logs_filter_by_status(self, tmp_path):
        db_dir = str(tmp_path / "status_filter_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.add_log(operation_type="create_user", target_name="U1", status="success")
            await Database.add_log(operation_type="create_user", target_name="U2", status="failed")
            await Database.add_log(operation_type="create_user", target_name="U3", status="success")

            result = await Database.get_logs(status="failed")
            assert result["total"] == 1
            assert result["logs"][0]["target_name"] == "U2"

    @pytest.mark.asyncio
    async def test_get_log_by_id(self, tmp_path):
        db_dir = str(tmp_path / "by_id_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.add_log(
                operation_type="create_user",
                target_dn="CN=Test,DC=corp,DC=com",
                target_name="TestUser",
                status="success",
                detail="test detail",
            )

            # Get the log id
            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT id FROM sync_logs WHERE target_name = 'TestUser'")
                log_id = (await cursor.fetchone())[0]

            log = await Database.get_log(log_id)
            assert log is not None
            assert log["target_name"] == "TestUser"
            assert log["operation_type"] == "create_user"
            assert log["detail"] == "test detail"

    @pytest.mark.asyncio
    async def test_get_log_nonexistent(self, tmp_path):
        db_dir = str(tmp_path / "nonexist_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            log = await Database.get_log(99999)
            assert log is None


class TestSyncStatusOperations:
    """Test sync status operations"""

    @pytest.mark.asyncio
    async def test_get_sync_status_default(self, tmp_path):
        db_dir = str(tmp_path / "sync_default_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            status = await Database.get_sync_status()
            assert status["is_running"] is False
            assert status["last_sync_status"] == "none"
            assert status["last_sync_total"] == 0

    @pytest.mark.asyncio
    async def test_update_sync_status_running(self, tmp_path):
        db_dir = str(tmp_path / "sync_update_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.update_sync_status(is_running=True, last_sync_status="running")

            status = await Database.get_sync_status()
            assert status["is_running"] is True
            assert status["last_sync_status"] == "running"

    @pytest.mark.asyncio
    async def test_update_sync_status_complete(self, tmp_path):
        db_dir = str(tmp_path / "sync_complete_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            await Database.update_sync_status(
                is_running=False,
                last_sync_time="2026-07-23T10:00:00",
                last_sync_status="success",
                last_sync_total=10,
                last_sync_success=8,
                last_sync_failed=2
            )

            status = await Database.get_sync_status()
            assert status["is_running"] is False
            assert status["last_sync_status"] == "success"
            assert status["last_sync_total"] == 10
            assert status["last_sync_success"] == 8
            assert status["last_sync_failed"] == 2

    @pytest.mark.asyncio
    async def test_update_sync_status_partial_update(self, tmp_path):
        """Should update only specified fields"""
        db_dir = str(tmp_path / "sync_partial_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            # Set initial state
            await Database.update_sync_status(is_running=True, last_sync_status="running")

            # Update only is_running
            await Database.update_sync_status(is_running=False)

            status = await Database.get_sync_status()
            assert status["is_running"] is False
            assert status["last_sync_status"] == "running"  # unchanged

    @pytest.mark.asyncio
    async def test_update_sync_status_no_params(self, tmp_path):
        """No parameters should be a no-op"""
        db_dir = str(tmp_path / "sync_noop_test")
        db_path = os.path.join(db_dir, "sync.db")

        with patch.object(database, "DB_DIR", db_dir), \
             patch.object(database, "DB_PATH", db_path):
            await init_db()

            # Should not raise
            await Database.update_sync_status()
