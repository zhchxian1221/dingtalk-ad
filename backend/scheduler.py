"""
定时任务调度模块
使用 APScheduler 实现定时同步
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import Database
from dingtalk_api import DingTalkClient
from ad_sync import ADSyncService, execute_sync

logger = logging.getLogger(__name__)

# 全局调度器实例
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

# 定时任务ID
SCHEDULE_JOB_ID = "dingtalk_ad_sync"


async def run_scheduled_sync():
    """定时同步任务执行函数"""
    logger.info("定时同步任务开始执行")

    # 检查是否已有同步在运行
    status = await Database.get_sync_status()
    if status.get("is_running"):
        logger.warning("已有同步任务正在运行，跳过本次定时同步")
        return

    # 获取配置
    config = await Database.get_all_config()
    app_key = config.get("dingtalk_app_key", "")
    app_secret = config.get("dingtalk_app_secret", "")

    if not app_key or not app_secret:
        logger.error("钉钉AppKey或AppSecret未配置，跳过定时同步")
        return

    # 创建客户端并执行同步
    dingtalk_client = DingTalkClient(app_key, app_secret)
    ad_service = ADSyncService(
        server=config.get("ad_server", ""),
        username=config.get("ad_username", ""),
        password=config.get("ad_password", ""),
        base_dn=config.get("ad_base_dn", "")
    )

    try:
        result = await execute_sync(dingtalk_client, ad_service, config, Database)
        logger.info(f"定时同步完成: {result}")
    except Exception as e:
        logger.error(f"定时同步失败: {e}")
    finally:
        await dingtalk_client.close()


async def setup_scheduler():
    """
    初始化定时任务调度器
    从数据库读取CRON配置，如果启用则添加定时任务
    """
    config = await Database.get_all_config()
    cron_expression = config.get("scheduler_cron", "0 2 * * *")
    enabled = config.get("scheduler_enabled", "false").lower() == "true"

    if enabled:
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            scheduler.add_job(
                run_scheduled_sync,
                trigger=trigger,
                id=SCHEDULE_JOB_ID,
                replace_existing=True,
                misfire_grace_time=3600
            )
            logger.info(f"定时同步任务已启用，CRON: {cron_expression}")
        except Exception as e:
            logger.error(f"定时任务配置失败: {e}")
    else:
        logger.info("定时同步任务未启用")


async def update_schedule(cron_expression: str, enabled: bool):
    """
    更新定时任务配置并重新调度

    Args:
        cron_expression: CRON表达式
        enabled: 是否启用
    """
    # 先移除现有任务
    try:
        scheduler.remove_job(SCHEDULE_JOB_ID)
    except Exception:
        pass  # 任务不存在时忽略

    # 保存配置到数据库
    await Database.set_config("scheduler_cron", cron_expression)
    await Database.set_config("scheduler_enabled", "true" if enabled else "false")

    # 如果启用，添加新任务
    if enabled:
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            scheduler.add_job(
                run_scheduled_sync,
                trigger=trigger,
                id=SCHEDULE_JOB_ID,
                replace_existing=True,
                misfire_grace_time=3600
            )
            logger.info(f"定时同步任务已更新，CRON: {cron_expression}")
        except Exception as e:
            logger.error(f"更新定时任务失败: {e}")
            raise Exception(f"CRON表达式无效: {e}")
    else:
        logger.info("定时同步任务已禁用")
