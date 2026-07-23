"""
Test suite for scheduler.py module
Tests: setup_scheduler, update_schedule, run_scheduled_sync, CRON parsing
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import scheduler as scheduler_module
from scheduler import scheduler, setup_scheduler, update_schedule, run_scheduled_sync, SCHEDULE_JOB_ID


class TestSetupScheduler:
    """Test setup_scheduler function"""

    @pytest.mark.asyncio
    async def test_setup_scheduler_disabled(self):
        """When scheduler_enabled is false, no job should be added"""
        mock_db = AsyncMock()
        mock_db.get_all_config.return_value = {
            "scheduler_cron": "0 2 * * *",
            "scheduler_enabled": "false",
        }

        mock_sched = MagicMock()
        mock_sched.add_job = MagicMock()

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.scheduler", mock_sched):
            await setup_scheduler()

            mock_sched.add_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_scheduler_enabled(self):
        """When scheduler_enabled is true, job should be added"""
        mock_db = AsyncMock()
        mock_db.get_all_config.return_value = {
            "scheduler_cron": "0 2 * * *",
            "scheduler_enabled": "true",
        }

        mock_sched = MagicMock()
        mock_sched.add_job = MagicMock()

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.scheduler", mock_sched):
            await setup_scheduler()

            mock_sched.add_job.assert_called_once()
            call_kwargs = mock_sched.add_job.call_args.kwargs
            assert call_kwargs["id"] == SCHEDULE_JOB_ID
            assert call_kwargs["replace_existing"] is True

    @pytest.mark.asyncio
    async def test_setup_scheduler_invalid_cron(self):
        """Invalid CRON expression should be caught and logged, not crash"""
        mock_db = AsyncMock()
        mock_db.get_all_config.return_value = {
            "scheduler_cron": "invalid cron",
            "scheduler_enabled": "true",
        }

        mock_sched = MagicMock()
        mock_sched.add_job = MagicMock(side_effect=Exception("Invalid cron"))

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.scheduler", mock_sched):
            # Should not raise
            await setup_scheduler()


class TestUpdateSchedule:
    """Test update_schedule function"""

    @pytest.mark.asyncio
    async def test_update_schedule_enable(self):
        """Enabling should add job and save config"""
        mock_db = AsyncMock()

        # Patch the global scheduler to avoid event loop issues
        mock_sched = MagicMock()
        mock_sched.add_job = MagicMock()
        mock_sched.remove_job = MagicMock()

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.scheduler", mock_sched):
            await update_schedule("0 3 * * *", True)

            mock_db.set_config.assert_any_call("scheduler_cron", "0 3 * * *")
            mock_db.set_config.assert_any_call("scheduler_enabled", "true")

            mock_sched.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_schedule_disable(self):
        """Disabling should remove job and save config"""
        mock_db = AsyncMock()

        mock_sched = MagicMock()
        mock_sched.add_job = MagicMock()
        mock_sched.remove_job = MagicMock()

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.scheduler", mock_sched):
            await update_schedule("0 3 * * *", False)

            mock_db.set_config.assert_any_call("scheduler_enabled", "false")
            mock_sched.remove_job.assert_called_once_with(SCHEDULE_JOB_ID)

    @pytest.mark.asyncio
    async def test_update_schedule_invalid_cron_raises(self):
        """Invalid CRON should raise exception"""
        mock_db = AsyncMock()

        with patch("scheduler.Database", mock_db):
            with pytest.raises(Exception, match="CRON"):
                await update_schedule("not a valid cron", True)


class TestRunScheduledSync:
    """Test run_scheduled_sync function"""

    @pytest.mark.asyncio
    async def test_run_scheduled_sync_already_running(self):
        """Should skip if sync is already running"""
        mock_db = AsyncMock()
        mock_db.get_sync_status.return_value = {"is_running": True}

        with patch("scheduler.Database", mock_db):
            await run_scheduled_sync()

            # Should not proceed to get config
            mock_db.get_all_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_scheduled_sync_no_credentials(self):
        """Should skip if no DingTalk credentials configured"""
        mock_db = AsyncMock()
        mock_db.get_sync_status.return_value = {"is_running": False}
        mock_db.get_all_config.return_value = {
            "dingtalk_app_key": "",
            "dingtalk_app_secret": "",
        }

        with patch("scheduler.Database", mock_db):
            await run_scheduled_sync()

            # Should not attempt to create clients
            mock_db.update_sync_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_scheduled_sync_with_credentials(self):
        """Should execute sync when credentials are present"""
        mock_db = AsyncMock()
        mock_db.get_sync_status.return_value = {"is_running": False}
        mock_db.get_all_config.return_value = {
            "dingtalk_app_key": "test_key",
            "dingtalk_app_secret": "test_secret",
            "ad_server": "192.168.1.1:389",
            "ad_username": "admin",
            "ad_password": "pass",
            "ad_base_dn": "OU=Users,DC=corp,DC=com",
            "initial_password": "Test@2026",
        }

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.execute_sync", new_callable=AsyncMock) as mock_execute:

            mock_execute.return_value = {"total": 0, "success": 0, "failed": 0, "status": "success"}

            await run_scheduled_sync()

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_scheduled_sync_handles_exception(self):
        """Should handle exceptions gracefully"""
        mock_db = AsyncMock()
        mock_db.get_sync_status.return_value = {"is_running": False}
        mock_db.get_all_config.return_value = {
            "dingtalk_app_key": "test_key",
            "dingtalk_app_secret": "test_secret",
            "ad_server": "192.168.1.1:389",
            "ad_username": "admin",
            "ad_password": "pass",
            "ad_base_dn": "OU=Users,DC=corp,DC=com",
            "initial_password": "Test@2026",
        }

        with patch("scheduler.Database", mock_db), \
             patch("scheduler.execute_sync", new_callable=AsyncMock) as mock_execute, \
             patch("scheduler.DingTalkClient") as mock_dt_class:

            mock_execute.side_effect = Exception("Sync failed")
            mock_client = AsyncMock()
            mock_dt_class.return_value = mock_client

            # Should not raise
            await run_scheduled_sync()
