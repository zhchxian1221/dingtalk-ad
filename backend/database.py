"""
数据库模块 - SQLite + aiosqlite
提供同步日志、配置、同步状态的数据持久化
"""

import os
import json
import aiosqlite
from datetime import datetime
from typing import Optional

# 数据库路径：优先使用环境变量，默认为代码同级目录下的 data 子目录
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.environ.get("DB_DIR", os.path.join(_BASE_DIR, "data"))
DB_PATH = os.path.join(DB_DIR, "sync.db")

# 默认配置项
DEFAULT_CONFIG = {
    "dingtalk_app_key": "",
    "dingtalk_app_secret": "",
    "ad_server": "",
    "ad_username": "",
    "ad_password": "",
    "ad_base_dn": "OU=Users,OU=REALMAN,DC=corp,DC=realman-robot,DC=com",
    "sync_strategy_disable": "true",
    "initial_password": "Realman@2026",
    "scheduler_cron": "0 2 * * *",
    "scheduler_enabled": "false",
    "ad_groups_ou": "",  # 安全组OU路径，空则自动从base_dn推导
}


async def init_db():
    """初始化数据库，创建表结构和默认配置"""
    os.makedirs(DB_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # 同步日志表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_time TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                target_dn TEXT,
                target_name TEXT,
                status TEXT NOT NULL,
                detail TEXT,
                error_message TEXT
            )
        """)

        # 配置表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # 同步状态表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id INTEGER PRIMARY KEY,
                last_sync_time TEXT,
                last_sync_status TEXT,
                last_sync_total INTEGER DEFAULT 0,
                last_sync_success INTEGER DEFAULT 0,
                last_sync_failed INTEGER DEFAULT 0,
                is_running INTEGER DEFAULT 0,
                last_error TEXT
            )
        """)
        # 兼容旧数据库：添加 last_error 列（如果不存在）
        try:
            await db.execute("ALTER TABLE sync_status ADD COLUMN last_error TEXT")
        except Exception:
            pass  # 列已存在

        # 兼容旧数据库：添加 sync_batch_id 列（如果不存在）
        try:
            await db.execute("ALTER TABLE sync_logs ADD COLUMN sync_batch_id TEXT")
        except Exception:
            pass  # 列已存在

        # 用户主部门覆盖表（管理员手动指定用户的主部门）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_primary_dept (
                userid TEXT PRIMARY KEY,
                primary_dept_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # 初始化默认配置
        for key, value in DEFAULT_CONFIG.items():
            await db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value)
            )

        # 初始化同步状态记录
        await db.execute("""
            INSERT OR IGNORE INTO sync_status (id, is_running, last_sync_status, last_sync_total, last_sync_success, last_sync_failed)
            VALUES (1, 0, 'none', 0, 0, 0)
        """)

        await db.commit()


class Database:
    """数据库操作类，所有方法均为静态异步方法"""

    # ==================== 配置操作 ====================

    @staticmethod
    async def get_config(key: str, default: Optional[str] = None) -> str:
        """获取单个配置项"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else (default if default is not None else DEFAULT_CONFIG.get(key, ""))

    @staticmethod
    async def set_config(key: str, value: str):
        """设置配置项"""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value)
            )
            await db.commit()

    @staticmethod
    async def get_all_config() -> dict:
        """获取所有配置项"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT key, value FROM config")
            rows = await cursor.fetchall()
            config = {row[0]: row[1] for row in rows}
            # 合并默认值
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config

    @staticmethod
    async def update_config(config_dict: dict):
        """批量更新配置项"""
        async with aiosqlite.connect(DB_PATH) as db:
            for key, value in config_dict.items():
                await db.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value))
                )
            await db.commit()

    # ==================== 日志操作 ====================

    @staticmethod
    async def add_log(
        operation_type: str,
        target_dn: str = "",
        target_name: str = "",
        status: str = "success",
        detail: str = "",
        error_message: str = "",
        sync_batch_id: str = ""
    ):
        """添加同步日志

        Args:
            operation_type: 操作类型（如 create_ou, create_user, sync_summary 等）
            target_dn: 目标对象的DN
            target_name: 目标对象名称
            status: 操作状态（success / failed / skipped）
            detail: 详情（JSON字符串或纯文本）
            error_message: 错误信息（失败时填写）
            sync_batch_id: 同步批次ID（同一次同步产生的日志共享同一个batch_id）
        """
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO sync_logs (sync_time, operation_type, target_dn, target_name, status, detail, error_message, sync_batch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                operation_type,
                target_dn,
                target_name,
                status,
                detail,
                error_message,
                sync_batch_id
            ))
            await db.commit()

    @staticmethod
    async def get_logs(
        page: int = 1,
        page_size: int = 20,
        operation_type: Optional[str] = None,
        status: Optional[str] = None,
        sync_batch_id: Optional[str] = None
    ) -> dict:
        """分页获取同步日志

        Args:
            page: 页码（从1开始）
            page_size: 每页条数
            operation_type: 操作类型筛选
            status: 状态筛选
            sync_batch_id: 同步批次ID筛选

        Returns:
            包含 logs 列表和分页信息的字典
        """
        offset = (page - 1) * page_size
        conditions = []
        params = []

        if operation_type:
            conditions.append("operation_type = ?")
            params.append(operation_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if sync_batch_id:
            conditions.append("sync_batch_id = ?")
            params.append(sync_batch_id)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        async with aiosqlite.connect(DB_PATH) as db:
            # 查询总数
            cursor = await db.execute(f"SELECT COUNT(*) FROM sync_logs{where_clause}", params)
            total = (await cursor.fetchone())[0]

            # 查询分页数据
            query = f"""
                SELECT id, sync_time, operation_type, target_dn, target_name, status, detail, error_message, sync_batch_id
                FROM sync_logs{where_clause}
                ORDER BY sync_time DESC
                LIMIT ? OFFSET ?
            """
            cursor = await db.execute(query, params + [page_size, offset])
            rows = await cursor.fetchall()

            logs = []
            for row in rows:
                logs.append({
                    "id": row[0],
                    "sync_time": row[1],
                    "operation_type": row[2],
                    "target_dn": row[3],
                    "target_name": row[4],
                    "status": row[5],
                    "detail": row[6],
                    "error_message": row[7],
                    "sync_batch_id": row[8] if len(row) > 8 else None
                })

            return {
                "logs": logs,
                "total": total,
                "page": page,
                "page_size": page_size
            }

    @staticmethod
    async def get_log(log_id: int) -> Optional[dict]:
        """获取单条日志详情"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, sync_time, operation_type, target_dn, target_name, status, detail, error_message, sync_batch_id FROM sync_logs WHERE id = ?",
                (log_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "sync_time": row[1],
                "operation_type": row[2],
                "target_dn": row[3],
                "target_name": row[4],
                "status": row[5],
                "detail": row[6],
                "error_message": row[7],
                "sync_batch_id": row[8] if len(row) > 8 else None
            }

    @staticmethod
    async def get_batch_summary(batch_id: str) -> Optional[dict]:
        """获取单次同步批次的统计摘要

        按操作类型分组统计 success / failed / skipped 计数，并返回总计。

        Args:
            batch_id: 同步批次ID

        Returns:
            批次统计字典，包含:
            - batch_id: 批次ID
            - sync_time: 批次开始时间
            - operations: {operation_type: {success: N, failed: N, skipped: N}}
            - total / success / failed / skipped: 汇总计数
            如果批次不存在则返回 None
        """
        async with aiosqlite.connect(DB_PATH) as db:
            # 验证批次存在并获取最早时间
            cursor = await db.execute(
                "SELECT sync_time FROM sync_logs WHERE sync_batch_id = ? ORDER BY sync_time ASC LIMIT 1",
                (batch_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None

            sync_time = row[0]

            # 按操作类型和状态分组统计（排除 sync_summary 摘要日志本身）
            cursor = await db.execute(
                "SELECT operation_type, status, COUNT(*) FROM sync_logs "
                "WHERE sync_batch_id = ? AND operation_type != 'sync_summary' "
                "GROUP BY operation_type, status",
                (batch_id,)
            )
            rows = await cursor.fetchall()

            operations: dict = {}
            total_count = 0
            total_success = 0
            total_failed = 0
            total_skipped = 0

            for op_type, op_status, count in rows:
                if op_type not in operations:
                    operations[op_type] = {"success": 0, "failed": 0, "skipped": 0}
                operations[op_type][op_status] = operations[op_type].get(op_status, 0) + count

                total_count += count
                if op_status == "success":
                    total_success += count
                elif op_status == "failed":
                    total_failed += count
                elif op_status == "skipped":
                    total_skipped += count

            return {
                "batch_id": batch_id,
                "sync_time": sync_time,
                "operations": operations,
                "total": total_count,
                "success": total_success,
                "failed": total_failed,
                "skipped": total_skipped,
            }

    @staticmethod
    async def get_recent_batches(limit: int = 20) -> list:
        """获取最近的同步批次列表

        按 sync_batch_id 分组，返回每个批次的统计摘要。

        Args:
            limit: 返回的最大批次数

        Returns:
            批次列表，每条含:
            - batch_id: 批次ID
            - sync_time: 批次开始时间
            - end_time: 批次结束时间
            - total / success / failed / skipped: 操作统计
            - status: 整体状态（success / partial / skipped / failed）
        """
        async with aiosqlite.connect(DB_PATH) as db:
            # 按 batch_id 分组获取批次列表
            cursor = await db.execute(
                """
                SELECT sync_batch_id,
                       MIN(sync_time) as start_time,
                       MAX(sync_time) as end_time,
                       COUNT(*) as total_ops
                FROM sync_logs
                WHERE sync_batch_id IS NOT NULL AND sync_batch_id != ''
                GROUP BY sync_batch_id
                ORDER BY start_time DESC
                LIMIT ?
                """,
                (limit,)
            )
            batch_rows = await cursor.fetchall()

            batches = []
            for batch_id, start_time, end_time, _total_ops in batch_rows:
                # 查询该批次的操作统计（按操作类型+状态分组，排除 sync_summary 摘要日志）
                cursor = await db.execute(
                    "SELECT operation_type, status, COUNT(*) FROM sync_logs "
                    "WHERE sync_batch_id = ? AND operation_type != 'sync_summary' "
                    "GROUP BY operation_type, status",
                    (batch_id,)
                )
                op_rows = await cursor.fetchall()

                success = 0
                failed = 0
                skipped = 0
                operations: dict = {}
                for op_type, op_status, cnt in op_rows:
                    if op_type not in operations:
                        operations[op_type] = {"success": 0, "failed": 0, "skipped": 0}
                    operations[op_type][op_status] = operations[op_type].get(op_status, 0) + cnt

                    if op_status == "success":
                        success += cnt
                    elif op_status == "failed":
                        failed += cnt
                    elif op_status == "skipped":
                        skipped += cnt

                actual_total = success + failed + skipped

                # 查询摘要日志获取整体状态
                cursor = await db.execute(
                    "SELECT status FROM sync_logs "
                    "WHERE sync_batch_id = ? AND operation_type = 'sync_summary' LIMIT 1",
                    (batch_id,)
                )
                summary_row = await cursor.fetchone()
                if summary_row:
                    overall_status = summary_row[0]
                elif failed > 0:
                    overall_status = "partial"
                else:
                    overall_status = "success"

                batches.append({
                    "batch_id": batch_id,
                    "sync_time": start_time,
                    "end_time": end_time,
                    "total": actual_total,
                    "success": success,
                    "failed": failed,
                    "skipped": skipped,
                    "status": overall_status,
                    "operations": operations,
                })

            return batches

    # ==================== 同步状态操作 ====================

    @staticmethod
    async def get_sync_status() -> dict:
        """获取同步状态"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT last_sync_time, last_sync_status, last_sync_total, last_sync_success, last_sync_failed, is_running, last_error FROM sync_status WHERE id = 1"
            )
            row = await cursor.fetchone()
            if not row:
                return {
                    "is_running": False,
                    "last_sync_time": None,
                    "last_sync_status": "none",
                    "last_sync_total": 0,
                    "last_sync_success": 0,
                    "last_sync_failed": 0,
                    "last_error": None
                }
            return {
                "is_running": bool(row[5]),
                "last_sync_time": row[0],
                "last_sync_status": row[1],
                "last_sync_total": row[2],
                "last_sync_success": row[3],
                "last_sync_failed": row[4],
                "last_error": row[6] if len(row) > 6 else None
            }

    @staticmethod
    async def update_sync_status(
        is_running: Optional[bool] = None,
        last_sync_time: Optional[str] = None,
        last_sync_status: Optional[str] = None,
        last_sync_total: Optional[int] = None,
        last_sync_success: Optional[int] = None,
        last_sync_failed: Optional[int] = None,
        last_error: Optional[str] = None
    ):
        """更新同步状态"""
        sets = []
        params = []

        if is_running is not None:
            sets.append("is_running = ?")
            params.append(1 if is_running else 0)
        if last_sync_time is not None:
            sets.append("last_sync_time = ?")
            params.append(last_sync_time)
        if last_sync_status is not None:
            sets.append("last_sync_status = ?")
            params.append(last_sync_status)
        if last_sync_total is not None:
            sets.append("last_sync_total = ?")
            params.append(last_sync_total)
        if last_sync_success is not None:
            sets.append("last_sync_success = ?")
            params.append(last_sync_success)
        if last_sync_failed is not None:
            sets.append("last_sync_failed = ?")
            params.append(last_sync_failed)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)

        if not sets:
            return

        params.append(1)  # WHERE id = 1
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                f"UPDATE sync_status SET {', '.join(sets)} WHERE id = ?",
                params
            )
            await db.commit()

    # ==================== 用户主部门操作 ====================

    @staticmethod
    async def get_user_primary_dept(userid: str) -> Optional[int]:
        """获取用户的主部门覆盖（如果有的话）"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT primary_dept_id FROM user_primary_dept WHERE userid = ?",
                (userid,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set_user_primary_dept(userid: str, dept_id: int):
        """设置用户的主部门覆盖"""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO user_primary_dept (userid, primary_dept_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(userid) DO UPDATE SET primary_dept_id = excluded.primary_dept_id, updated_at = excluded.updated_at",
                (userid, dept_id, datetime.now().isoformat())
            )
            await db.commit()

    @staticmethod
    async def get_all_primary_depts() -> dict:
        """获取所有用户的主部门覆盖"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT userid, primary_dept_id FROM user_primary_dept")
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

    @staticmethod
    async def delete_user_primary_dept(userid: str):
        """删除用户的主部门覆盖（恢复为自动判断）"""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM user_primary_dept WHERE userid = ?", (userid,))
            await db.commit()

    @staticmethod
    async def clear_all_data():
        """清空所有同步相关数据（用于重建）"""
        async with aiosqlite.connect(DB_PATH) as db:
            # 清空同步日志
            await db.execute("DELETE FROM sync_logs")
            # 清空用户主部门覆盖
            await db.execute("DELETE FROM user_primary_dept")
            # 重置同步状态
            await db.execute("""
                UPDATE sync_status
                SET last_sync_time = NULL,
                    last_sync_status = 'none',
                    last_sync_total = 0,
                    last_sync_success = 0,
                    last_sync_failed = 0,
                    is_running = 0,
                    last_error = NULL
                WHERE id = 1
            """)
            await db.commit()
