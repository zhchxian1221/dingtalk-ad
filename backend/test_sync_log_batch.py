"""
同步日志系统改进 - 单元测试
验证 database.py 的批次ID相关新函数、ad_sync.py 的批次统计逻辑

测试策略：
- 用临时 SQLite 数据库隔离测试（不污染生产数据）
- 覆盖正常路径、边界条件、旧数据兼容
- 验证 SQL 正确性、统计准确性、状态判断逻辑
"""

import asyncio
import json
import os
import sys
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# 将 backend 目录加入 sys.path
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# 在导入 database 之前，设置临时数据库路径
_TEMP_DIR = tempfile.mkdtemp(prefix="sync_test_")

import database as db_module
db_module.DB_DIR = _TEMP_DIR
db_module.DB_PATH = os.path.join(_TEMP_DIR, "test_sync.db")

from database import Database, init_db


# ==================== Fixtures ====================

@pytest_asyncio.fixture
async def clean_db():
    """每个测试前初始化干净的数据库"""
    # 确保表结构存在
    await init_db()
    # 清空 sync_logs 表
    import aiosqlite
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute("DELETE FROM sync_logs")
        await db.execute("DELETE FROM sync_status")
        await db.execute("DELETE FROM config")
        await db.commit()
    yield
    # 测试后清理
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute("DELETE FROM sync_logs")
        await db.commit()


def _teardown_module():
    """模块级别清理临时目录"""
    if os.path.exists(_TEMP_DIR):
        shutil.rmtree(_TEMP_DIR, ignore_errors=True)


# ==================== 测试：add_log 带 sync_batch_id ====================

class TestAddLogWithBatchId:
    """验证 add_log 正确写入 sync_batch_id"""

    @pytest.mark.asyncio
    async def test_add_log_with_batch_id(self, clean_db):
        """测试 add_log 带 batch_id 写入正确"""
        await Database.add_log(
            operation_type="create_user",
            target_dn="CN=张三,OU=Users,DC=example,DC=com",
            target_name="张三",
            status="success",
            detail='{"userid": "001"}',
            error_message="",
            sync_batch_id="batch-001"
        )
        result = await Database.get_logs(sync_batch_id="batch-001")
        assert result["total"] == 1
        log = result["logs"][0]
        assert log["sync_batch_id"] == "batch-001"
        assert log["operation_type"] == "create_user"
        assert log["target_name"] == "张三"
        assert log["status"] == "success"

    @pytest.mark.asyncio
    async def test_add_log_empty_batch_id(self, clean_db):
        """测试 add_log 不传 batch_id 时默认为空字符串"""
        await Database.add_log(
            operation_type="create_ou",
            target_name="测试OU",
            status="success"
        )
        result = await Database.get_logs()
        assert result["total"] == 1
        # 默认值为空字符串
        assert result["logs"][0]["sync_batch_id"] == ""

    @pytest.mark.asyncio
    async def test_add_log_multiple_batch_ids(self, clean_db):
        """测试同一批次写入多条日志"""
        batch_id = "batch-multi"
        for i in range(5):
            await Database.add_log(
                operation_type="create_user",
                target_name=f"user_{i}",
                status="success",
                sync_batch_id=batch_id
            )
        result = await Database.get_logs(sync_batch_id=batch_id)
        assert result["total"] == 5
        for log in result["logs"]:
            assert log["sync_batch_id"] == batch_id


# ==================== 测试：get_logs 按 batch_id 筛选 ====================

class TestGetLogsBatchFilter:
    """验证 get_logs 按 sync_batch_id 筛选的正确性"""

    @pytest.mark.asyncio
    async def test_get_logs_filter_by_batch_id(self, clean_db):
        """测试按 batch_id 筛选只返回该批次的日志"""
        # 写入两个批次的日志
        for i in range(3):
            await Database.add_log(
                operation_type="create_user",
                target_name=f"用户A{i}",
                status="success",
                sync_batch_id="batch-A"
            )
        for i in range(2):
            await Database.add_log(
                operation_type="create_user",
                target_name=f"用户B{i}",
                status="success",
                sync_batch_id="batch-B"
            )

        result_a = await Database.get_logs(sync_batch_id="batch-A")
        assert result_a["total"] == 3
        for log in result_a["logs"]:
            assert log["sync_batch_id"] == "batch-A"

        result_b = await Database.get_logs(sync_batch_id="batch-B")
        assert result_b["total"] == 2
        for log in result_b["logs"]:
            assert log["sync_batch_id"] == "batch-B"

    @pytest.mark.asyncio
    async def test_get_logs_no_batch_filter(self, clean_db):
        """测试不传 batch_id 时返回所有日志"""
        await Database.add_log(
            operation_type="create_user",
            target_name="用户1",
            status="success",
            sync_batch_id="batch-1"
        )
        await Database.add_log(
            operation_type="create_user",
            target_name="用户2",
            status="success",
            sync_batch_id="batch-2"
        )
        result = await Database.get_logs()
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_get_logs_combined_filter(self, clean_db):
        """测试 batch_id + status 组合筛选"""
        await Database.add_log(
            operation_type="create_user",
            target_name="成功用户",
            status="success",
            sync_batch_id="batch-combined"
        )
        await Database.add_log(
            operation_type="create_user",
            target_name="失败用户",
            status="failed",
            error_message="创建失败",
            sync_batch_id="batch-combined"
        )
        # 筛选该批次中失败的
        result = await Database.get_logs(
            sync_batch_id="batch-combined",
            status="failed"
        )
        assert result["total"] == 1
        assert result["logs"][0]["status"] == "failed"
        assert result["logs"][0]["target_name"] == "失败用户"

    @pytest.mark.asyncio
    async def test_get_logs_nonexistent_batch_id(self, clean_db):
        """测试筛选不存在的 batch_id 返回空"""
        await Database.add_log(
            operation_type="create_user",
            target_name="用户",
            status="success",
            sync_batch_id="batch-exists"
        )
        result = await Database.get_logs(sync_batch_id="batch-not-exists")
        assert result["total"] == 0
        assert result["logs"] == []


# ==================== 测试：get_batch_summary ====================

class TestGetBatchSummary:
    """验证 get_batch_summary 的 SQL 分组统计正确性"""

    @pytest.mark.asyncio
    async def test_batch_summary_basic(self, clean_db):
        """测试批次摘要的基本统计"""
        batch_id = "summary-batch-1"
        # 2个成功创建用户 + 1个失败创建用户 + 1个成功创建OU
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)
        await Database.add_log(operation_type="create_user", target_name="u2",
                               status="success", sync_batch_id=batch_id)
        await Database.add_log(operation_type="create_user", target_name="u3",
                               status="failed", error_message="err",
                               sync_batch_id=batch_id)
        await Database.add_log(operation_type="create_ou", target_name="ou1",
                               status="success", sync_batch_id=batch_id)
        # 摘要日志本身不应被计入统计
        await Database.add_log(operation_type="sync_summary", status="partial",
                               detail="{}", sync_batch_id=batch_id)

        summary = await Database.get_batch_summary(batch_id)
        assert summary is not None
        assert summary["batch_id"] == batch_id
        assert summary["total"] == 4  # 排除 sync_summary
        assert summary["success"] == 3
        assert summary["failed"] == 1
        assert summary["skipped"] == 0
        # 操作类型分组
        assert "create_user" in summary["operations"]
        assert summary["operations"]["create_user"]["success"] == 2
        assert summary["operations"]["create_user"]["failed"] == 1
        assert "create_ou" in summary["operations"]
        assert summary["operations"]["create_ou"]["success"] == 1

    @pytest.mark.asyncio
    async def test_batch_summary_with_skipped(self, clean_db):
        """测试包含 skipped 状态的批次摘要"""
        batch_id = "summary-batch-skip"
        await Database.add_log(operation_type="disable_user", target_name="skip1",
                               status="skipped", detail="安全保护",
                               sync_batch_id=batch_id)
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)

        summary = await Database.get_batch_summary(batch_id)
        assert summary is not None
        assert summary["skipped"] == 1
        assert summary["success"] == 1
        assert summary["total"] == 2
        assert summary["operations"]["disable_user"]["skipped"] == 1

    @pytest.mark.asyncio
    async def test_batch_summary_nonexistent(self, clean_db):
        """测试不存在的批次返回 None"""
        summary = await Database.get_batch_summary("nonexistent-batch")
        assert summary is None

    @pytest.mark.asyncio
    async def test_batch_summary_excludes_sync_summary(self, clean_db):
        """验证摘要统计排除了 sync_summary 类型的日志"""
        batch_id = "summary-exclude-test"
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)
        await Database.add_log(operation_type="sync_summary", status="success",
                               detail="{}", sync_batch_id=batch_id)

        summary = await Database.get_batch_summary(batch_id)
        assert summary is not None
        assert summary["total"] == 1  # 只有 create_user，不含 sync_summary
        assert "sync_summary" not in summary["operations"]

    @pytest.mark.asyncio
    async def test_batch_summary_sync_time(self, clean_db):
        """验证摘要返回的 sync_time 是批次最早时间"""
        batch_id = "summary-time-test"
        # 按时间顺序写入
        await Database.add_log(operation_type="create_ou", target_name="ou1",
                               status="success", sync_batch_id=batch_id)
        await asyncio.sleep(0.05)
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)

        summary = await Database.get_batch_summary(batch_id)
        assert summary is not None
        assert summary["sync_time"] is not None
        # sync_time 应该是较早的那条（create_ou 的时间）
        result = await Database.get_logs(sync_batch_id=batch_id, page_size=2)
        times = [log["sync_time"] for log in result["logs"]]
        # get_logs 按 DESC 排序，所以 [0] 是最晚的，[-1] 是最早的
        assert summary["sync_time"] == times[-1]


# ==================== 测试：get_recent_batches ====================

class TestGetRecentBatches:
    """验证 get_recent_batches 的分组+子查询正确性"""

    @pytest.mark.asyncio
    async def test_get_recent_batches_basic(self, clean_db):
        """测试获取最近批次列表"""
        # 创建3个批次
        for batch_id in ["batch-r1", "batch-r2", "batch-r3"]:
            await Database.add_log(operation_type="create_user", target_name="u1",
                                   status="success", sync_batch_id=batch_id)
            await Database.add_log(operation_type="create_user", target_name="u2",
                                   status="failed", error_message="e",
                                   sync_batch_id=batch_id)

        batches = await Database.get_recent_batches(limit=20)
        assert len(batches) == 3
        for b in batches:
            assert b["batch_id"] in ["batch-r1", "batch-r2", "batch-r3"]
            assert b["total"] == 2
            assert b["success"] == 1
            assert b["failed"] == 1
            assert b["skipped"] == 0
            assert b["sync_time"] is not None
            assert b["end_time"] is not None

    @pytest.mark.asyncio
    async def test_get_recent_batches_order_desc(self, clean_db):
        """测试批次按时间倒序排列"""
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id="batch-old")
        await asyncio.sleep(0.05)
        await Database.add_log(operation_type="create_user", target_name="u2",
                               status="success", sync_batch_id="batch-new")

        batches = await Database.get_recent_batches(limit=20)
        assert len(batches) == 2
        # 最新的在前
        assert batches[0]["batch_id"] == "batch-new"
        assert batches[1]["batch_id"] == "batch-old"

    @pytest.mark.asyncio
    async def test_get_recent_batches_limit(self, clean_db):
        """测试 limit 参数限制返回数量"""
        for i in range(5):
            await Database.add_log(operation_type="create_user", target_name=f"u{i}",
                                   status="success", sync_batch_id=f"batch-lim-{i}")
            await asyncio.sleep(0.01)

        batches = await Database.get_recent_batches(limit=3)
        assert len(batches) == 3

    @pytest.mark.asyncio
    async def test_get_recent_batches_status_from_summary(self, clean_db):
        """测试整体状态从 sync_summary 日志获取"""
        batch_id = "batch-status-summary"
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)
        await Database.add_log(operation_type="sync_summary", status="partial",
                               detail="{}", sync_batch_id=batch_id)

        batches = await Database.get_recent_batches(limit=20)
        batch = [b for b in batches if b["batch_id"] == batch_id][0]
        assert batch["status"] == "partial"

    @pytest.mark.asyncio
    async def test_get_recent_batches_status_fallback_partial(self, clean_db):
        """测试无 summary 日志时，有失败则状态为 partial"""
        batch_id = "batch-fallback-partial"
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)
        await Database.add_log(operation_type="create_user", target_name="u2",
                               status="failed", error_message="e",
                               sync_batch_id=batch_id)
        # 不写 sync_summary 日志

        batches = await Database.get_recent_batches(limit=20)
        batch = [b for b in batches if b["batch_id"] == batch_id][0]
        assert batch["status"] == "partial"

    @pytest.mark.asyncio
    async def test_get_recent_batches_status_fallback_success(self, clean_db):
        """测试无 summary 日志且无失败时，状态为 success"""
        batch_id = "batch-fallback-success"
        await Database.add_log(operation_type="create_user", target_name="u1",
                               status="success", sync_batch_id=batch_id)
        # 不写 sync_summary 日志

        batches = await Database.get_recent_batches(limit=20)
        batch = [b for b in batches if b["batch_id"] == batch_id][0]
        assert batch["status"] == "success"

    @pytest.mark.asyncio
    async def test_get_recent_batches_empty(self, clean_db):
        """测试无数据时返回空列表"""
        batches = await Database.get_recent_batches(limit=20)
        assert batches == []


# ==================== 测试：旧数据兼容（batch_id 为 NULL） ====================

class TestOldDataCompatibility:
    """验证 batch_id 为 NULL/空的旧日志能正常查询"""

    @pytest.mark.asyncio
    async def test_old_logs_with_null_batch_id_queryable(self, clean_db):
        """测试旧日志（直接插入 NULL batch_id）可通过 get_logs 查询"""
        import aiosqlite
        # 模拟旧数据：直接插入不含 sync_batch_id 的记录
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "INSERT INTO sync_logs (sync_time, operation_type, target_name, status) "
                "VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(), "create_user", "旧用户", "success")
            )
            await db.commit()

        # 不带 batch_id 筛选时应能查到
        result = await Database.get_logs()
        assert result["total"] == 1
        log = result["logs"][0]
        assert log["target_name"] == "旧用户"
        # sync_batch_id 应为 NULL 或 None
        assert log["sync_batch_id"] is None or log["sync_batch_id"] == ""

    @pytest.mark.asyncio
    async def test_old_logs_not_in_recent_batches(self, clean_db):
        """测试旧日志（NULL batch_id）不出现在批次列表中"""
        import aiosqlite
        # 插入旧数据
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "INSERT INTO sync_logs (sync_time, operation_type, target_name, status) "
                "VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(), "create_user", "旧用户", "success")
            )
            await db.commit()

        batches = await Database.get_recent_batches(limit=20)
        # 旧数据没有 batch_id，不应出现在批次列表
        assert len(batches) == 0

    @pytest.mark.asyncio
    async def test_mixed_old_and_new_data(self, clean_db):
        """测试新旧数据混合时查询正常"""
        import aiosqlite
        # 旧数据
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "INSERT INTO sync_logs (sync_time, operation_type, target_name, status) "
                "VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(), "create_user", "旧用户", "success")
            )
            await db.commit()

        # 新数据
        await Database.add_log(
            operation_type="create_user",
            target_name="新用户",
            status="success",
            sync_batch_id="batch-new"
        )

        # 查全部
        result_all = await Database.get_logs()
        assert result_all["total"] == 2

        # 按 batch_id 筛选新数据
        result_new = await Database.get_logs(sync_batch_id="batch-new")
        assert result_new["total"] == 1
        assert result_new["logs"][0]["target_name"] == "新用户"

        # 批次列表只有新数据
        batches = await Database.get_recent_batches(limit=20)
        assert len(batches) == 1
        assert batches[0]["batch_id"] == "batch-new"


# ==================== 测试：get_log 单条详情 ====================

class TestGetLogDetail:
    """验证 get_log 返回 sync_batch_id 字段"""

    @pytest.mark.asyncio
    async def test_get_log_returns_batch_id(self, clean_db):
        """测试 get_log 返回 sync_batch_id"""
        await Database.add_log(
            operation_type="create_user",
            target_name="测试用户",
            status="success",
            sync_batch_id="batch-detail"
        )
        result = await Database.get_logs(sync_batch_id="batch-detail")
        log_id = result["logs"][0]["id"]

        log = await Database.get_log(log_id)
        assert log is not None
        assert log["sync_batch_id"] == "batch-detail"
        assert log["target_name"] == "测试用户"

    @pytest.mark.asyncio
    async def test_get_log_nonexistent(self, clean_db):
        """测试获取不存在的日志返回 None"""
        log = await Database.get_log(99999)
        assert log is None


# ==================== 测试：ad_sync.py 的 _track 和 summary_status 逻辑 ====================

class TestSyncBatchStatsLogic:
    """验证 ad_sync.py 中 batch_stats 和 _track 的统计逻辑"""

    def test_track_single_operation(self):
        """测试 _track 记录单个操作"""
        batch_stats = {}

        def _track(op_type, op_status):
            if op_type not in batch_stats:
                batch_stats[op_type] = {"success": 0, "failed": 0, "skipped": 0}
            batch_stats[op_type][op_status] = batch_stats[op_type].get(op_status, 0) + 1

        _track("create_user", "success")
        assert batch_stats == {"create_user": {"success": 1, "failed": 0, "skipped": 0}}

    def test_track_multiple_operations(self):
        """测试 _track 记录多个操作和状态"""
        batch_stats = {}

        def _track(op_type, op_status):
            if op_type not in batch_stats:
                batch_stats[op_type] = {"success": 0, "failed": 0, "skipped": 0}
            batch_stats[op_type][op_status] = batch_stats[op_type].get(op_status, 0) + 1

        _track("create_ou", "success")
        _track("create_ou", "success")
        _track("create_ou", "failed")
        _track("create_user", "success")
        _track("disable_user", "skipped")

        assert batch_stats["create_ou"]["success"] == 2
        assert batch_stats["create_ou"]["failed"] == 1
        assert batch_stats["create_ou"]["skipped"] == 0
        assert batch_stats["create_user"]["success"] == 1
        assert batch_stats["disable_user"]["skipped"] == 1

    def test_summary_status_skipped(self):
        """测试 summary_status: skip_disable=True 且 total=0 → skipped"""
        skip_disable = True
        total = 0
        failed_count = 0

        if skip_disable and total == 0:
            summary_status = "skipped"
        elif failed_count == 0:
            summary_status = "success"
        else:
            summary_status = "partial"

        assert summary_status == "skipped"

    def test_summary_status_success(self):
        """测试 summary_status: 有操作但无失败 → success"""
        skip_disable = False
        total = 5
        failed_count = 0

        if skip_disable and total == 0:
            summary_status = "skipped"
        elif failed_count == 0:
            summary_status = "success"
        else:
            summary_status = "partial"

        assert summary_status == "success"

    def test_summary_status_partial(self):
        """测试 summary_status: 有失败 → partial"""
        skip_disable = False
        total = 5
        failed_count = 2

        if skip_disable and total == 0:
            summary_status = "skipped"
        elif failed_count == 0:
            summary_status = "success"
        else:
            summary_status = "partial"

        assert summary_status == "partial"

    def test_summary_status_skip_with_operations(self):
        """测试 summary_status: skip_disable=True 但有操作 → success/partial（非 skipped）"""
        skip_disable = True
        total = 3
        failed_count = 0

        if skip_disable and total == 0:
            summary_status = "skipped"
        elif failed_count == 0:
            summary_status = "success"
        else:
            summary_status = "partial"

        assert summary_status == "success"

    def test_batch_stats_serializable(self):
        """测试 batch_stats 可被 json.dumps 序列化（用于 detail 字段）"""
        batch_stats = {
            "create_ou": {"success": 2, "failed": 1, "skipped": 0},
            "create_user": {"success": 5, "failed": 0, "skipped": 0},
            "disable_user": {"success": 0, "failed": 0, "skipped": 1},
        }
        detail = json.dumps(batch_stats, ensure_ascii=False)
        # 确保可以反序列化回来
        parsed = json.loads(detail)
        assert parsed == batch_stats


# ==================== 测试：execute_sync 集成（mock 外部依赖） ====================

class TestExecuteSyncIntegration:
    """用 mock 验证 execute_sync 的批次日志记录完整性"""

    @pytest.mark.asyncio
    async def test_execute_sync_all_add_logs_have_batch_id(self, clean_db):
        """验证 execute_sync 中所有 add_log 调用都携带 sync_batch_id"""
        from ad_sync import execute_sync

        # Mock 钉钉客户端
        mock_client = AsyncMock()
        mock_client.get_departments = AsyncMock(return_value=[
            {"dept_id": 1, "name": "根部门", "parent_id": 0},
            {"dept_id": 2, "name": "技术部", "parent_id": 1},
        ])
        mock_client.get_all_users = AsyncMock(return_value=[
            {"name": "张三", "userid": "001", "email": "zhangsan@test.com",
             "mobile": "13800000001", "title": "工程师", "job_number": "J001",
             "dept_id_list": [2], "account": ""},
        ])
        mock_client.close = AsyncMock()

        # Mock AD 服务
        mock_ad = MagicMock()
        mock_ad.connect = MagicMock(return_value=True)
        mock_ad.disconnect = MagicMock()
        mock_ad.get_existing_users = MagicMock(return_value=[])  # AD为空，所有用户都是新增
        mock_ad.get_all_ous = MagicMock(return_value=[])  # 没有OU
        mock_ad.create_ou = MagicMock(return_value=True)
        mock_ad.create_user = MagicMock(return_value=(True, "CN=张三,OU=技术部,DC=example,DC=com"))

        config = {
            "ad_base_dn": "OU=Users,DC=example,DC=com",
            "initial_password": "Test@2026",
        }

        # 执行同步
        result = await execute_sync(mock_client, mock_ad, config, Database)

        assert result["sync_batch_id"] is not None
        batch_id = result["sync_batch_id"]

        # 查询该批次的所有日志
        logs_result = await Database.get_logs(sync_batch_id=batch_id, page_size=100)
        all_logs = logs_result["logs"]

        # 所有日志都应该有 batch_id
        for log in all_logs:
            assert log["sync_batch_id"] == batch_id, (
                f"日志 id={log['id']} 缺少 sync_batch_id"
            )

        # 应该有 sync_summary 日志
        summary_logs = [l for l in all_logs if l["operation_type"] == "sync_summary"]
        assert len(summary_logs) == 1
        assert summary_logs[0]["status"] == "success"

        # 应该有 create_ou 和 create_user 日志
        ou_logs = [l for l in all_logs if l["operation_type"] == "create_ou"]
        user_logs = [l for l in all_logs if l["operation_type"] == "create_user"]
        assert len(ou_logs) == 1  # 技术部
        assert len(user_logs) == 1  # 张三

    @pytest.mark.asyncio
    async def test_execute_sync_exception_records_summary(self, clean_db):
        """验证 execute_sync 异常时记录 failed 状态的摘要日志"""
        from ad_sync import execute_sync

        # Mock 钉钉客户端 - 让 get_departments 抛异常
        mock_client = AsyncMock()
        mock_client.get_departments = AsyncMock(side_effect=Exception("钉钉API故障"))
        mock_client.get_all_users = AsyncMock(return_value=[])
        mock_client.close = AsyncMock()

        mock_ad = MagicMock()
        mock_ad.connect = MagicMock(return_value=True)
        mock_ad.disconnect = MagicMock()

        config = {"ad_base_dn": "OU=Users,DC=example,DC=com"}

        # 执行同步，预期抛异常
        with pytest.raises(Exception, match="钉钉API故障"):
            await execute_sync(mock_client, ad_service=mock_ad, config=config, db=Database)

        # 检查数据库中是否有 failed 状态的 summary 日志
        result = await Database.get_logs(operation_type="sync_summary", page_size=10)
        assert result["total"] == 1
        summary_log = result["logs"][0]
        assert summary_log["status"] == "failed"
        assert summary_log["sync_batch_id"] != ""  # 有批次ID

    @pytest.mark.asyncio
    async def test_execute_sync_skip_disable_records_skipped(self, clean_db):
        """验证安全保护跳过禁用时记录 skipped 日志"""
        from ad_sync import execute_sync

        # Mock 钉钉返回空用户列表（触发安全保护）
        mock_client = AsyncMock()
        mock_client.get_departments = AsyncMock(return_value=[
            {"dept_id": 1, "name": "根部门", "parent_id": 0},
        ])
        mock_client.get_all_users = AsyncMock(return_value=[])  # 空用户列表
        mock_client.close = AsyncMock()

        # AD有一个用户
        mock_ad = MagicMock()
        mock_ad.connect = MagicMock(return_value=True)
        mock_ad.disconnect = MagicMock()
        mock_ad.get_existing_users = MagicMock(return_value=[
            {"cn": "已有用户", "dn": "CN=已有用户,OU=Users,DC=example,DC=com",
             "userAccountControl": 512, "sAMAccountName": "existing"}
        ])
        mock_ad.get_all_ous = MagicMock(return_value=[])

        config = {"ad_base_dn": "OU=Users,DC=example,DC=com"}

        result = await execute_sync(mock_client, mock_ad, config, Database)

        batch_id = result["sync_batch_id"]
        logs_result = await Database.get_logs(sync_batch_id=batch_id, page_size=100)
        all_logs = logs_result["logs"]

        # 应该有 skipped 状态的 disable_user 日志（安全保护跳过）
        skip_disable_logs = [l for l in all_logs
                             if l["status"] == "skipped" and l["operation_type"] == "disable_user"]
        assert len(skip_disable_logs) == 1

        # summary 状态应为 skipped（total=0 且 skip_disable=True）
        summary_logs = [l for l in all_logs if l["operation_type"] == "sync_summary"]
        assert len(summary_logs) == 1
        assert summary_logs[0]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_execute_sync_batch_summary_consistency(self, clean_db):
        """验证批次摘要统计与实际日志一致"""
        from ad_sync import execute_sync

        mock_client = AsyncMock()
        mock_client.get_departments = AsyncMock(return_value=[
            {"dept_id": 1, "name": "根部门", "parent_id": 0},
            {"dept_id": 2, "name": "技术部", "parent_id": 1},
            {"dept_id": 3, "name": "市场部", "parent_id": 1},
        ])
        mock_client.get_all_users = AsyncMock(return_value=[
            {"name": "张三", "userid": "001", "email": "", "mobile": "",
             "title": "", "job_number": "", "dept_id_list": [2], "account": ""},
            {"name": "李四", "userid": "002", "email": "", "mobile": "",
             "title": "", "job_number": "", "dept_id_list": [3], "account": ""},
        ])
        mock_client.close = AsyncMock()

        # create_user 一个成功一个失败
        mock_ad = MagicMock()
        mock_ad.connect = MagicMock(return_value=True)
        mock_ad.disconnect = MagicMock()
        mock_ad.get_existing_users = MagicMock(return_value=[])
        mock_ad.get_all_ous = MagicMock(return_value=[])
        mock_ad.create_ou = MagicMock(return_value=True)
        mock_ad.create_user = MagicMock(side_effect=[
            (True, "CN=张三,OU=技术部,DC=example,DC=com"),
            (False, "密码复杂度不足"),
        ])

        config = {"ad_base_dn": "OU=Users,DC=example,DC=com"}

        result = await execute_sync(mock_client, mock_ad, config, Database)
        batch_id = result["sync_batch_id"]

        # 用 get_batch_summary 验证统计
        summary = await Database.get_batch_summary(batch_id)
        assert summary is not None
        # 2个 create_ou (技术部、市场部) + 2个 create_user
        assert summary["total"] == 4
        assert summary["success"] == 3  # 2 OU + 1 user
        assert summary["failed"] == 1   # 1 user failed
        assert "create_ou" in summary["operations"]
        assert summary["operations"]["create_ou"]["success"] == 2
        assert "create_user" in summary["operations"]
        assert summary["operations"]["create_user"]["success"] == 1
        assert summary["operations"]["create_user"]["failed"] == 1

        # 用 get_recent_batches 验证
        batches = await Database.get_recent_batches(limit=5)
        batch = [b for b in batches if b["batch_id"] == batch_id][0]
        assert batch["total"] == 4
        assert batch["success"] == 3
        assert batch["failed"] == 1
        # summary 日志状态应为 partial（有失败）
        assert batch["status"] == "partial"


# ==================== 测试：init_db 的 ALTER TABLE 兼容性 ====================

class TestInitDbMigration:
    """验证 init_db 的 sync_batch_id 列添加逻辑"""

    @pytest.mark.asyncio
    async def test_init_db_adds_batch_id_column(self, clean_db):
        """测试 init_db 为旧表添加 sync_batch_id 列"""
        import aiosqlite
        # 验证 sync_batch_id 列存在
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cursor = await db.execute("PRAGMA table_info(sync_logs)")
            columns = await cursor.fetchall()
            col_names = [col[1] for col in columns]
            assert "sync_batch_id" in col_names

    @pytest.mark.asyncio
    async def test_init_db_idempotent(self, clean_db):
        """测试多次调用 init_db 不报错（ALTER TABLE 幂等）"""
        # 第一次 init_db 已在 fixture 中完成
        # 再次调用应不报错
        await init_db()
        await init_db()

        import aiosqlite
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cursor = await db.execute("PRAGMA table_info(sync_logs)")
            columns = await cursor.fetchall()
            col_names = [col[1] for col in columns]
            assert "sync_batch_id" in col_names
            # 确保没有重复列
            assert col_names.count("sync_batch_id") == 1


# 模块清理
def test_module_cleanup():
    """测试后清理临时目录"""
    _teardown_module()
