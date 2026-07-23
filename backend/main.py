"""
FastAPI 应用入口
提供所有API路由 + 静态文件服务
钉钉 → AD域控同步系统后端
"""

import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import Database, init_db
from dingtalk_api import DingTalkClient
from ad_sync import ADSyncService, preview_sync, execute_sync, get_groups_ou_path
from scheduler import scheduler, setup_scheduler, update_schedule

# 日志配置
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(LOG_DIR, "app.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB 单文件上限
            backupCount=10,  # 保留最近10个备份
            encoding="utf-8"
        )
    ]
)
logger = logging.getLogger(__name__)

# 静态文件目录（兼容Docker和本地开发）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_BASE_DIR, "static")
if not os.path.exists(STATIC_DIR):
    STATIC_DIR = os.path.join(_BASE_DIR, "..", "frontend")


# ==================== 生命周期管理 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库和调度器，关闭时停止调度器"""
    logger.info("应用启动中...")
    await init_db()
    await setup_scheduler()
    scheduler.start()
    logger.info("应用启动完成")
    yield
    logger.info("应用关闭中...")
    scheduler.shutdown(wait=False)
    logger.info("应用已关闭")


# ==================== FastAPI 应用 ====================

app = FastAPI(title="钉钉AD同步系统", version="1.0.0", lifespan=lifespan)

# 全局异常处理：确保所有异常返回JSON而非HTML
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"未捕获的异常: {type(exc).__name__}: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "msg": f"服务器内部错误: {str(exc)}", "data": None}
    )

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 响应工具函数 ====================

def success(data=None, msg: str = "success"):
    """成功响应"""
    return {"code": 200, "msg": msg, "data": data}


def error(msg: str = "error", code: int = 400):
    """错误响应"""
    return {"code": code, "msg": msg, "data": None}


# ==================== 请求模型 ====================

class ConfigUpdate(BaseModel):
    """配置更新模型"""
    dingtalk_app_key: Optional[str] = None
    dingtalk_app_secret: Optional[str] = None
    ad_server: Optional[str] = None
    ad_username: Optional[str] = None
    ad_password: Optional[str] = None
    ad_base_dn: Optional[str] = None
    ad_groups_ou: Optional[str] = None
    sync_strategy_disable: Optional[str] = None
    initial_password: Optional[str] = None


class SchedulerUpdate(BaseModel):
    """定时任务更新模型"""
    cron_expression: Optional[str] = None
    enabled: Optional[bool] = None


class SyncExecuteRequest(BaseModel):
    """同步执行请求"""
    dry_run: bool = False


class PrimaryDeptUpdate(BaseModel):
    """主部门更新模型"""
    primary_dept_id: int


# ==================== 客户端工厂 ====================

async def create_dingtalk_client() -> DingTalkClient:
    """从数据库配置创建钉钉客户端"""
    config = await Database.get_all_config()
    app_key = config.get("dingtalk_app_key", "")
    app_secret = config.get("dingtalk_app_secret", "")
    if not app_key or not app_secret:
        raise ValueError("请先配置钉钉AppKey和AppSecret")
    return DingTalkClient(app_key, app_secret)


async def create_ad_service() -> ADSyncService:
    """从数据库配置创建AD服务"""
    config = await Database.get_all_config()
    ad_server = config.get("ad_server", "")
    if not ad_server:
        raise ValueError("请先配置AD服务器地址")
    return ADSyncService(
        server=ad_server,
        username=config.get("ad_username", ""),
        password=config.get("ad_password", ""),
        base_dn=config.get("ad_base_dn", "OU=Users,DC=example,DC=com")
    )


# ==================== API 路由 ====================

# ---------- 健康检查 ----------

@app.get("/api/health")
async def health_check():
    """健康检查"""
    return success({"status": "ok", "time": datetime.now().isoformat()})


# ---------- 配置管理 ----------

@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    try:
        config = await Database.get_all_config()
        return success(config)
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return error(f"获取配置失败: {str(e)}")


@app.put("/api/config")
async def update_config(config: ConfigUpdate):
    """更新配置"""
    try:
        update_data = {k: v for k, v in config.model_dump(exclude_none=True).items() if v is not None}
        if update_data:
            await Database.update_config(update_data)
        return success(msg="配置已更新")
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        return error(f"更新配置失败: {str(e)}")


# ---------- 钉钉数据 ----------

@app.get("/api/dingtalk/departments")
async def get_dingtalk_departments():
    """获取钉钉部门列表"""
    try:
        client = await create_dingtalk_client()
        try:
            departments = await client.get_departments()
            return success({"departments": departments, "count": len(departments)})
        finally:
            await client.close()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"获取钉钉部门列表失败: {e}")
        return error(f"获取钉钉部门列表失败: {str(e)}")


@app.get("/api/dingtalk/users")
async def get_dingtalk_users():
    """获取钉钉用户列表"""
    try:
        client = await create_dingtalk_client()
        try:
            users = await client.get_all_users()
            return success({"users": users, "count": len(users)})
        finally:
            await client.close()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"获取钉钉用户列表失败: {e}")
        return error(f"获取钉钉用户列表失败: {str(e)}")


@app.post("/api/dingtalk/test")
async def test_dingtalk_connection():
    """测试钉钉连接"""
    try:
        client = await create_dingtalk_client()
        try:
            token = await client.get_access_token()
            if token:
                return success(msg="钉钉连接成功")
            else:
                return error("钉钉连接失败：无法获取access_token")
        finally:
            await client.close()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"钉钉连接测试失败: {e}")
        return error(f"钉钉连接失败: {str(e)}")


# ---------- AD数据 ----------

@app.get("/api/ad/users")
async def get_ad_users():
    """获取AD现有用户列表"""
    try:
        ad_service = await create_ad_service()
        try:
            ad_service.connect()
            users = ad_service.get_existing_users()
            return success({"users": users, "count": len(users)})
        finally:
            ad_service.disconnect()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"获取AD用户列表失败: {e}")
        return error(f"获取AD用户列表失败: {str(e)}")


@app.post("/api/ad/test")
async def test_ad_connection():
    """测试AD连接（含加密状态检测）"""
    try:
        ad_service = await create_ad_service()
        result = ad_service.test_connection()
        if result["success"]:
            return success({
                "secured": result["secured"],
                "message": result["message"]
            })
        else:
            return error(result["message"])
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"AD连接测试失败: {e}")
        return error(f"AD连接失败: {str(e)}")


@app.post("/api/ad/clear-all")
async def clear_all_ad_data():
    """清空AD中所有由本工具创建的数据（用户、OU、安全组）并清空本地数据库记录"""
    try:
        # 检查同步状态
        status = await Database.get_sync_status()
        if status.get("is_running"):
            return error("同步正在进行中，请等待完成后再清空")

        ad_service = await create_ad_service()
        config = await Database.get_all_config()
        base_dn = config.get("ad_base_dn", "OU=Users,DC=example,DC=com")
        groups_ou = get_groups_ou_path(base_dn, config)

        try:
            ad_service.connect()
            result = await asyncio.to_thread(ad_service.clear_all_data, base_dn, groups_ou)
            await Database.clear_all_data()
            logger.info(f"清空重建完成: {result}")
            return success({
                "msg": "清空完成",
                "ad_result": result
            })
        finally:
            ad_service.disconnect()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"清空AD数据失败: {e}")
        return error(f"清空失败: {str(e)}")


# ---------- 同步操作 ----------

@app.post("/api/sync/preview")
async def sync_preview():
    """预览同步差异"""
    try:
        # 检查同步状态
        status = await Database.get_sync_status()
        if status.get("is_running"):
            return error("同步正在进行中，请等待完成后再预览")

        client = await create_dingtalk_client()
        ad_service = await create_ad_service()
        config = await Database.get_all_config()

        try:
            result = await preview_sync(client, ad_service, config, Database)
            return success(result)
        finally:
            await client.close()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"预览同步差异失败: {e}")
        return error(f"预览同步差异失败: {str(e)}")


@app.post("/api/sync/execute")
async def sync_execute(req: SyncExecuteRequest):
    """执行同步（后台异步执行）"""
    try:
        # 检查同步状态
        status = await Database.get_sync_status()
        if status.get("is_running"):
            return error("同步正在进行中，请等待完成")

        # 后台执行同步
        asyncio.create_task(_run_sync_background())

        return success(msg="同步已启动，请关注同步状态")
    except Exception as e:
        logger.error(f"启动同步失败: {e}")
        return error(f"启动同步失败: {str(e)}")


async def _run_sync_background():
    """后台执行同步任务"""
    try:
        client = await create_dingtalk_client()
        ad_service = await create_ad_service()
        config = await Database.get_all_config()

        try:
            result = await execute_sync(client, ad_service, config, Database)
            logger.info(f"后台同步完成: {result}")
            # 同步成功，清除上次错误
            await Database.update_sync_status(last_error=None)
        finally:
            await client.close()
    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        error_trace = traceback.format_exc()
        logger.error(f"后台同步异常:\n{error_trace}")
        await Database.update_sync_status(
            is_running=False,
            last_sync_time=datetime.now().isoformat(),
            last_sync_status="failed",
            last_sync_total=0,
            last_sync_success=0,
            last_sync_failed=0,
            last_error=error_msg
        )


@app.get("/api/sync/status")
async def get_sync_status():
    """获取当前同步状态"""
    try:
        status = await Database.get_sync_status()
        return success(status)
    except Exception as e:
        logger.error(f"获取同步状态失败: {e}")
        return error(f"获取同步状态失败: {str(e)}")


# ---------- 多部门管理 ----------

@app.get("/api/multi-dept/users")
async def get_multi_dept_users():
    """获取多部门用户列表（只返回属于多个部门的用户）"""
    try:
        client = await create_dingtalk_client()
        try:
            departments = await client.get_departments()
            users = await client.get_all_users()
            overrides = await Database.get_all_primary_depts()

            dept_map = {d["dept_id"]: d for d in departments}

            multi_dept_users = []
            for user in users:
                dept_id_list = user.get("dept_id_list", [])
                if len(dept_id_list) <= 1:
                    continue

                userid = user.get("userid", "")
                override_dept = overrides.get(userid)
                primary_dept_id = override_dept if override_dept else (dept_id_list[0] if dept_id_list else 1)

                dept_names = []
                for did in dept_id_list:
                    dept_info = dept_map.get(did)
                    if dept_info:
                        dept_names.append({
                            "dept_id": did,
                            "dept_name": dept_info["name"],
                        })

                multi_dept_users.append({
                    "userid": userid,
                    "name": user.get("name", ""),
                    "account": user.get("account", ""),
                    "email": user.get("email", ""),
                    "dept_list": dept_names,
                    "dept_count": len(dept_id_list),
                    "primary_dept_id": primary_dept_id,
                    "primary_dept_name": dept_map.get(primary_dept_id, {}).get("name", ""),
                    "is_override": userid in overrides,
                })

            return success({"users": multi_dept_users, "count": len(multi_dept_users)})
        finally:
            await client.close()
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        logger.error(f"获取多部门用户列表失败: {e}")
        return error(f"获取多部门用户列表失败: {str(e)}")


@app.put("/api/multi-dept/{userid}/primary")
async def set_primary_dept(userid: str, req: PrimaryDeptUpdate):
    """设置用户的主部门"""
    try:
        await Database.set_user_primary_dept(userid, req.primary_dept_id)
        return success(msg="主部门已更新")
    except Exception as e:
        logger.error(f"设置主部门失败: {e}")
        return error(f"设置主部门失败: {str(e)}")


@app.delete("/api/multi-dept/{userid}/primary")
async def delete_primary_dept(userid: str):
    """删除用户的主部门覆盖（恢复自动判断）"""
    try:
        await Database.delete_user_primary_dept(userid)
        return success(msg="已恢复自动判断主部门")
    except Exception as e:
        logger.error(f"删除主部门覆盖失败: {e}")
        return error(f"删除主部门覆盖失败: {str(e)}")


# ---------- 同步日志 ----------

@app.get("/api/logs")
async def get_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    operation_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None)
):
    """获取同步日志列表（分页）"""
    try:
        result = await Database.get_logs(
            page=page,
            page_size=page_size,
            operation_type=operation_type,
            status=status
        )
        return success(result)
    except Exception as e:
        logger.error(f"获取同步日志失败: {e}")
        return error(f"获取同步日志失败: {str(e)}")


@app.get("/api/logs/{log_id}")
async def get_log_detail(log_id: int):
    """获取单条日志详情"""
    log = await Database.get_log(log_id)
    if log:
        return success(log)
    return error("日志不存在", 404)


# ---------- 定时任务 ----------

@app.get("/api/scheduler")
async def get_scheduler_config():
    """获取定时任务配置"""
    try:
        config = await Database.get_all_config()
        return success({
            "cron_expression": config.get("scheduler_cron", "0 2 * * *"),
            "enabled": config.get("scheduler_enabled", "false").lower() == "true"
        })
    except Exception as e:
        logger.error(f"获取定时任务配置失败: {e}")
        return error(f"获取定时任务配置失败: {str(e)}")


@app.put("/api/scheduler")
async def update_scheduler_config(req: SchedulerUpdate):
    """更新定时任务配置"""
    try:
        cron_expr = req.cron_expression
        enabled = req.enabled

        # 获取当前配置作为默认值
        current_config = await Database.get_all_config()
        if cron_expr is None:
            cron_expr = current_config.get("scheduler_cron", "0 2 * * *")
        if enabled is None:
            enabled = current_config.get("scheduler_enabled", "false").lower() == "true"

        await update_schedule(cron_expr, enabled)
        return success(msg="定时任务配置已更新")
    except Exception as e:
        return error(f"更新定时任务失败: {str(e)}")


# ==================== 日志查看 ====================

@app.get("/api/logs/file")
async def get_file_logs(lines: int = Query(100, ge=10, le=500)):
    """查看文件日志（最近N行）"""
    try:
        log_path = os.path.join(LOG_DIR, "app.log")
        if not os.path.exists(log_path):
            return success({"lines": [], "message": "日志文件尚未生成"})

        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return success({
            "lines": [line.rstrip("\n") for line in recent],
            "total_lines": len(all_lines),
            "shown_lines": len(recent)
        })
    except Exception as e:
        return error(f"读取日志失败: {str(e)}")


# ==================== 静态文件服务 ====================

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    """返回前端首页"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(
        {"code": 200, "msg": "钉钉AD同步系统API", "data": {"docs": "/docs"}},
        media_type="application/json"
    )


@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    """捕获所有其他路径，返回前端首页（SPA支持）"""
    # 排除API路径
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API not found")
    # 排除静态文件路径
    if full_path.startswith("static/"):
        raise HTTPException(status_code=404, detail="File not found")

    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
