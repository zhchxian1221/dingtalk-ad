"""
钉钉API客户端模块
使用 httpx 异步调用钉钉旧版API（oapi.dingtalk.com）
功能：获取access_token、部门列表、用户列表
"""

import time
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

DINGTALK_BASE_URL = "https://oapi.dingtalk.com"


class DingTalkClient:
    """钉钉API客户端"""

    def __init__(self, app_key: str, app_secret: str):
        """
        初始化钉钉客户端

        Args:
            app_key: 钉钉应用的AppKey
            app_secret: 钉钉应用的AppSecret
        """
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token: Optional[str] = None
        self.token_expires: float = 0  # token过期时间戳
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_access_token(self) -> str:
        """
        获取access_token，带缓存机制
        token有效期7200秒，提前5分钟刷新

        Returns:
            access_token字符串

        Raises:
            Exception: 获取token失败时抛出异常
        """
        # 检查缓存是否有效（提前5分钟刷新）
        if self.access_token and time.time() < self.token_expires - 300:
            return self.access_token

        url = f"{DINGTALK_BASE_URL}/gettoken"
        params = {
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }

        resp = await self.client.get(url, params=params)
        data = resp.json()

        if data.get("errcode") != 0:
            error_msg = data.get("errmsg", "未知错误")
            logger.error(f"获取钉钉access_token失败: {error_msg}")
            raise Exception(f"获取钉钉access_token失败: {error_msg}")

        self.access_token = data["access_token"]
        self.token_expires = time.time() + data.get("expires_in", 7200)
        logger.info("钉钉access_token获取成功")
        return self.access_token

    async def get_departments(self) -> list[dict]:
        """
        递归获取所有部门列表，从根部门(id=1)开始

        Returns:
            部门列表，每个元素包含: dept_id, name, parent_id 等
        """
        token = await self.get_access_token()
        all_departments = []

        # 从根部门开始递归获取子部门
        await self._get_sub_departments(token, 1, all_departments)

        logger.info(f"获取到 {len(all_departments)} 个钉钉部门")
        return all_departments

    async def _get_sub_departments(
        self, token: str, parent_id: int, result: list[dict]
    ):
        """递归获取子部门"""
        url = f"{DINGTALK_BASE_URL}/topapi/v2/department/listsub"
        params = {"access_token": token}
        body = {"dept_id": parent_id}

        resp = await self.client.post(url, params=params, json=body)
        data = resp.json()

        if data.get("errcode") != 0:
            error_msg = data.get("errmsg", "未知错误")
            logger.error(f"获取部门列表失败(dept_id={parent_id}): {error_msg}")
            return

        departments = data.get("result", [])
        for dept in departments:
            dept_info = {
                "dept_id": dept.get("dept_id"),
                "name": dept.get("name", ""),
                "parent_id": dept.get("parent_id", parent_id),
                "create_dept_group": dept.get("create_dept_group", False),
                "auto_add_user": dept.get("auto_add_user", False),
            }
            result.append(dept_info)
            # 递归获取子部门
            await self._get_sub_departments(token, dept["dept_id"], result)

    async def get_department_users(self, dept_id: int) -> list[dict]:
        """
        获取指定部门的用户列表（分页获取）

        Args:
            dept_id: 部门ID

        Returns:
            用户列表，每个元素包含: userid, name, mobile, email, title, job_number, dept_id_list 等
        """
        token = await self.get_access_token()
        url = f"{DINGTALK_BASE_URL}/topapi/v2/user/list"
        params = {"access_token": token}

        all_users = []
        cursor = 0
        size = 100  # 每页100条

        while True:
            body = {
                "dept_id": dept_id,
                "cursor": cursor,
                "size": size
            }

            resp = await self.client.post(url, params=params, json=body)
            data = resp.json()

            if data.get("errcode") != 0:
                error_msg = data.get("errmsg", "未知错误")
                logger.error(f"获取部门用户失败(dept_id={dept_id}): {error_msg}")
                break

            result = data.get("result", {})
            user_list = result.get("list", [])
            has_more = result.get("has_more", False)

            for user in user_list:
                user_info = {
                    "userid": user.get("userid", ""),
                    "name": user.get("name", ""),
                    "mobile": user.get("mobile", ""),
                    "email": user.get("email", ""),
                    "title": user.get("title", ""),
                    "job_number": user.get("job_number", ""),
                    "dept_id_list": user.get("dept_id_list", []),
                    "active": user.get("active", True),
                    "position": user.get("position", ""),
                    "avatar": user.get("avatar", ""),
                    "hired_date": user.get("hired_date", 0),
                    "account": user.get("account", ""),  # 认证登录账号
                }
                all_users.append(user_info)

            if not has_more:
                break

            cursor = result.get("next_cursor", cursor + size)

        return all_users

    async def get_all_users(self) -> list[dict]:
        """
        遍历所有部门获取所有用户，按userid去重

        Returns:
            去重后的用户列表
        """
        departments = await self.get_departments()
        all_users = []
        seen_userids = set()  # 用于去重

        for dept in departments:
            dept_id = dept["dept_id"]
            users = await self.get_department_users(dept_id)
            for user in users:
                userid = user["userid"]
                if userid and userid not in seen_userids:
                    seen_userids.add(userid)
                    all_users.append(user)

        dept_count = len(departments)
        user_count = len(all_users)
        logger.info(f"获取到 {user_count} 个钉钉用户（去重后），共遍历 {dept_count} 个部门")

        if user_count == 0 and dept_count > 0:
            logger.warning(f"遍历了{dept_count}个部门但未获取到任何用户，可能是钉钉API异常（限流/Token过期/权限不足）")

        return all_users

    async def close(self):
        """关闭HTTP客户端"""
        await self.client.aclose()
