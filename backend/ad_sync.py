"""
AD域LDAP操作模块
使用 ldap3 库操作AD域控
功能：创建OU、用户、修改属性、移动、禁用/启用账号、同步逻辑
"""

import asyncio
import json
import re
import ssl
import subprocess
import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4
from ldap3 import Server, Connection, ALL, MODIFY_REPLACE, MODIFY_ADD, MODIFY_DELETE, SUBTREE, BASE, LEVEL, Tls
from ldap3.core.exceptions import LDAPException
from pypinyin import lazy_pinyin

logger = logging.getLogger(__name__)

def escape_dn_value(value: str) -> str:
    """
    转义LDAP DN中的特殊字符

    Args:
        value: 原始值

    Returns:
        转义后的值
    """
    # 注意顺序：先转义反斜杠
    value = value.replace("\\", "\\\\")
    value = value.replace(",", "\\,")
    value = value.replace("+", "\\+")
    value = value.replace("<", "\\<")
    value = value.replace(">", "\\>")
    value = value.replace(";", "\\;")
    value = value.replace('"', '\\"')
    value = value.replace("=", "\\=")
    return value

def get_domain_from_base_dn(base_dn: str) -> str:
    """
    从Base DN中提取域名

    例: OU=Users,DC=example,DC=com → example.com

    Args:
        base_dn: LDAP Base DN

    Returns:
        域名字符串
    """
    parts = base_dn.split(",")
    dc_parts = []
    for part in parts:
        part = part.strip()
        if part.upper().startswith("DC="):
            dc_parts.append(part[3:])
    return ".".join(dc_parts)

def clean_sam_account_name(raw: str) -> str:
    """
    清理字符串使其符合AD sAMAccountName的规范
    - 转小写
    - 移除空格
    - 只保留字母、数字、点、下划线、短横线
    - 不能以点结尾
    - 截断到20字符（sAMAccountName传统限制）

    Args:
        raw: 原始字符串

    Returns:
        清理后的字符串，如果清理后为空则返回空字符串
    """
    if not raw:
        return ""
    result = raw.strip().lower().replace(" ", "")
    result = re.sub(r'[^a-z0-9._-]', '', result)
    result = result.rstrip('.')
    if len(result) > 20:
        result = result[:20]
    return result

def chinese_to_pinyin(text: str) -> str:
    """
    将中文文本转换为拼音（无声调）
    非中文字符（英文、数字、符号）保留原样（大小写不变）
    注意：英文的小写化和去空格由下游 clean_sam_account_name() 负责

    例如：
    - "张三" → "zhangsan"
    - "张三001" → "zhangsan001"
    - "李四-测试" → "lisi-ceshi"
    - "Zhang San" → "Zhang San"（英文原样保留，不转小写不去空格）

    Args:
        text: 可能包含中文的文本

    Returns:
        拼音字符串，中文部分无声调
    """
    if not text:
        return ""
    return "".join(lazy_pinyin(text))

def generate_sam_account_name(user_data: dict) -> str:
    """
    生成AD用户的sAMAccountName（登录名）

    优先级：
    1. 邮箱前缀（@前面的部分，清理后使用）
    2. 钉钉account字段（认证登录账号）
       - 纯ASCII：直接清理
       - 含中文：先转拼音再清理
    3. 用户姓名（name字段）→ 转拼音 → 清理
    4. userid（最后兜底，保证唯一但不好记）

    Args:
        user_data: 用户数据字典，包含 email, account, userid, name 等字段

    Returns:
        sAMAccountName字符串
    """
    # 优先级1：邮箱前缀
    email = user_data.get("email", "").strip()
    if email and "@" in email:
        prefix = email.split("@")[0]
        cleaned = clean_sam_account_name(prefix)
        if cleaned:
            return cleaned

    # 优先级2：钉钉account字段（认证登录账号）
    account = user_data.get("account", "").strip()
    if account:
        # 如果account含中文字符，先转拼音
        if re.search(r'[\u4e00-\u9fff]', account):
            account = chinese_to_pinyin(account)
        cleaned = clean_sam_account_name(account)
        if cleaned:
            return cleaned

    # 优先级3：用户姓名转拼音
    name = user_data.get("name", "").strip()
    if name:
        pinyin_name = chinese_to_pinyin(name)
        cleaned = clean_sam_account_name(pinyin_name)
        if cleaned:
            return cleaned

    # 优先级4：userid（最后兜底）
    userid = user_data.get("userid", "").strip()
    if userid:
        cleaned = clean_sam_account_name(userid)
        if cleaned:
            return cleaned

    # 最终兜底：用name的hash生成
    return f"user{abs(hash(name)) % 100000}"

def get_dept_ou_path(dept_id: int, dept_map: dict, base_dn: str) -> str:
    """
    递归计算部门在AD中对应的OU完整路径

    根部门(parent_id为0或id为1)对应base_dn本身，子部门在其父部门OU下创建子OU

    Args:
        dept_id: 部门ID
        dept_map: 部门映射 {dept_id: dept_info}
        base_dn: AD根OU的DN

    Returns:
        部门对应的OU完整DN路径
    """
    if dept_id not in dept_map:
        return base_dn

    dept = dept_map[dept_id]
    parent_id = dept.get("parent_id", 0)
    if parent_id is None:
        parent_id = 0

    # 根部门：parent_id 为 0，或 parent_id 等于自身（自引用），或钉钉标准 dept_id 为 1
    if parent_id == 0 or parent_id == dept_id or dept_id == 1:
        return base_dn

    parent_path = get_dept_ou_path(parent_id, dept_map, base_dn)
    ou_name = escape_dn_value(dept["name"])
    return f"OU={ou_name},{parent_path}"

def get_dept_depth(dept_id: int, dept_map: dict) -> int:
    """
    计算部门在组织架构中的层级深度（根部门深度为0）

    Args:
        dept_id: 部门ID
        dept_map: 部门映射

    Returns:
        层级深度整数
    """
    depth = 0
    current = dept_id
    visited = set()
    while current in dept_map and current not in visited:
        visited.add(current)
        parent_id = dept_map[current].get("parent_id", 0)
        if parent_id is None:
            parent_id = 0
        if parent_id == 0 or parent_id == current or current == 1:
            break
        depth += 1
        current = parent_id
    return depth

def encode_ad_password(password: str) -> bytes:
    """
    将密码编码为AD的unicodePwd属性格式
    AD要求密码用双引号包裹并使用UTF-16LE编码

    Args:
        password: 明文密码

    Returns:
        编码后的密码字节串
    """
    return f'"{password}"'.encode("utf-16-le")

class ADSyncService:
    """AD域LDAP操作服务"""

    def __init__(self, server: str, username: str, password: str, base_dn: str):
        """
        初始化AD同步服务

        Args:
            server: AD服务器地址（如 192.168.1.10:389 或 ldaps://192.168.1.10:636）
            username: 管理员账号（如 administrator@your-domain.com）
            password: 密码
            base_dn: 根OU的DN（如 OU=Users,DC=example,DC=com）
        """
        self.server_str = server
        self.username = username
        self.password = password
        self.base_dn = base_dn
        self.conn: Optional[Connection] = None
        self.connection_secured = False  # 连接是否经过加密（SSL或StartTLS）

        # 解析服务器地址，判断是否使用SSL
        use_ssl = False
        host = server
        port = 389

        if server.startswith("ldaps://"):
            use_ssl = True
            host = server[8:]
        elif server.startswith("ldap://"):
            host = server[7:]

        if ":" in host:
            parts = host.split(":")
            host = parts[0]
            port = int(parts[1])

        if port == 636:
            use_ssl = True

        self.use_ssl = use_ssl
        self.tls_config = Tls(validate=ssl.CERT_NONE, version=ssl.PROTOCOL_TLS_CLIENT)
        self.server_obj = Server(host, port=port, use_ssl=use_ssl, tls=self.tls_config, get_info=ALL)

    def connect(self) -> bool:
        """
        连接AD服务器，自动尝试加密连接

        流程：
        1. 如果配置了 LDAPS (636) → 直接用 SSL
        2. 如果配置了 LDAP (389) → 先绑定，再尝试 StartTLS 升级为加密连接

        Returns:
            连接是否成功
        """
        try:
            self.conn = Connection(
                self.server_obj,
                user=self.username,
                password=self.password,
                auto_bind=True,
                receive_timeout=30
            )
            if not self.conn.bound:
                return False

            # 如果是 LDAPS，连接已经是加密的
            if self.use_ssl:
                self.connection_secured = True
                logger.info(f"AD连接成功 (LDAPS加密): {self.server_str}")
                return True

            # 尝试 StartTLS 升级为加密连接
            try:
                tls_started = self.conn.start_tls(tls=self.tls_config)
                if tls_started:
                    self.connection_secured = True
                    logger.info(f"AD连接成功 (StartTLS加密): {self.server_str}")
                else:
                    logger.warning(f"StartTLS启动失败，密码设置功能可能不可用: {self.server_str}")
            except Exception as e:
                logger.warning(f"StartTLS尝试失败: {e}，密码设置功能可能不可用")

            return True
        except LDAPException as e:
            logger.error(f"AD连接失败: {e}")
            return False

    def disconnect(self):
        """断开AD连接"""
        if self.conn:
            try:
                self.conn.unbind()
            except Exception:
                pass
            self.conn = None

    def _ensure_connection(self):
        """确保连接有效"""
        if not self.conn or not self.conn.bound:
            if not self.connect():
                raise Exception("AD连接失败，请检查AD服务器配置")

    def test_connection(self) -> dict:
        """
        测试LDAP连接，检测加密状态

        Returns:
            {success: bool, secured: bool, message: str}
        """
        try:
            conn = Connection(
                self.server_obj,
                user=self.username,
                password=self.password,
                auto_bind=True,
                receive_timeout=10
            )
            if not conn.bound:
                conn.unbind()
                return {"success": False, "secured": False, "message": "AD连接失败：无法绑定"}

            secured = self.use_ssl

            # 如果不是 LDAPS，尝试 StartTLS
            if not secured:
                try:
                    if conn.start_tls(tls=self.tls_config):
                        secured = True
                except Exception:
                    pass

            conn.unbind()

            if secured:
                return {"success": True, "secured": True, "message": "AD连接成功（加密连接），支持设置密码"}
            else:
                return {"success": True, "secured": False, "message": "AD连接成功（非加密），设置密码可能失败，建议使用 ldaps:// 地址"}
        except LDAPException as e:
            logger.error(f"AD连接测试失败: {e}")
            return {"success": False, "secured": False, "message": f"AD连接失败: {str(e)}"}

    def set_password_via_smb(self, sam_account_name: str, new_password: str) -> bool:
        """
        通过 SMB/RPC (SAMR) 协议设置 AD 用户密码
        适用于 LDAPS/StartTLS 不可用的情况，绕过 LDAP 加密要求

        Args:
            sam_account_name: 用户的 sAMAccountName
            new_password: 新密码

        Returns:
            是否成功
        """
        admin_user = self.username
        admin_pass = self.password
        dc_host = self.server_obj.host

        # net rpc password 通过 DCE-RPC 操作域控，不依赖 LDAP 加密
        cmd = [
            "net", "rpc", "password",
            sam_account_name,
            new_password,
            "-U", f"{admin_user}%{admin_pass}",
            "-S", dc_host
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info(f"SMB密码设置成功: {sam_account_name}")
                return True
            else:
                stderr = result.stderr.strip() or result.stdout.strip()
                logger.error(f"SMB密码设置失败: {sam_account_name}, stderr={stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"SMB密码设置超时: {sam_account_name}")
            return False
        except FileNotFoundError:
            logger.error("net 命令不可用，请确认 samba-common-bin 已安装")
            return False
        except Exception as e:
            logger.error(f"SMB命令执行异常: {e}")
            return False

    def get_all_ous(self) -> list[str]:
        """
        获取base_dn下所有OU的DN列表

        Returns:
            OU的DN字符串列表
        """
        self._ensure_connection()
        ous = []
        try:
            self.conn.search(
                search_base=self.base_dn,
                search_filter="(objectClass=organizationalUnit)",
                search_scope=SUBTREE,
                attributes=["distinguishedName"]
            )
            for entry in self.conn.entries:
                ous.append(str(entry.distinguishedName))
        except LDAPException as e:
            logger.error(f"获取OU列表失败: {e}")
        return ous

    def get_existing_users(self) -> list[dict]:
        """
        查询base_dn下所有用户

        Returns:
            用户列表，每个元素包含: dn, cn, displayName, mail, mobile, title, userAccountControl 等
        """
        self._ensure_connection()
        users = []
        try:
            self.conn.search(
                search_base=self.base_dn,
                search_filter="(&(objectClass=user)(objectCategory=person))",
                search_scope=SUBTREE,
                attributes=[
                    "cn", "displayName", "mail", "mobile", "title",
                    "employeeID", "userAccountControl", "distinguishedName",
                    "sAMAccountName", "userPrincipalName"
                ]
            )
            for entry in self.conn.entries:
                user = {
                    "dn": self._safe_attr(entry, "distinguishedName"),
                    "cn": self._safe_attr(entry, "cn"),
                    "displayName": self._safe_attr(entry, "displayName"),
                    "mail": self._safe_attr(entry, "mail"),
                    "mobile": self._safe_attr(entry, "mobile"),
                    "title": self._safe_attr(entry, "title"),
                    "employeeID": self._safe_attr(entry, "employeeID"),
                    "userAccountControl": int(self._safe_attr(entry, "userAccountControl") or "512"),
                    "sAMAccountName": self._safe_attr(entry, "sAMAccountName"),
                    "userPrincipalName": self._safe_attr(entry, "userPrincipalName"),
                }
                users.append(user)
        except LDAPException as e:
            logger.error(f"获取AD用户列表失败: {e}")

        logger.info(f"获取到 {len(users)} 个AD用户")
        return users

    def _safe_attr(self, entry, attr: str) -> str:
        """安全获取LDAP属性值"""
        try:
            val = entry[attr].value
            if val is not None:
                return str(val)
        except Exception:
            pass
        return ""

    def create_ou(self, ou_name: str, parent_dn: str) -> bool:
        """
        创建OU（组织单元）

        Args:
            ou_name: OU名称
            parent_dn: 父DN

        Returns:
            是否创建成功
        """
        self._ensure_connection()
        escaped_name = escape_dn_value(ou_name)
        ou_dn = f"OU={escaped_name},{parent_dn}"

        try:
            result = self.conn.add(
                ou_dn,
                attributes={
                    "objectClass": ["top", "organizationalUnit"],
                    "ou": ou_name,
                    "name": ou_name,
                }
            )
            if result:
                logger.info(f"创建OU成功: {ou_dn}")
                return True
            else:
                # 如果OU已存在，视为成功
                if "already exists" in str(self.conn.result.get("message", "")).lower():
                    logger.info(f"OU已存在: {ou_dn}")
                    return True
                logger.error(f"创建OU失败: {ou_dn}, {self.conn.result}")
                return False
        except LDAPException as e:
            logger.error(f"创建OU异常: {ou_dn}, {e}")
            return False

    def create_user(self, user_data: dict, parent_dn: str) -> tuple[bool, str]:
        """
        创建AD用户

        Args:
            user_data: 用户数据，包含 name, userid, mobile, email, title, job_number, initial_password
            parent_dn: 父OU的DN

        Returns:
            (是否成功, DN或错误信息)
        """
        self._ensure_connection()

        cn = user_data.get("name", "")
        sam_account_name = generate_sam_account_name(user_data)
        domain = get_domain_from_base_dn(self.base_dn)
        upn = f"{sam_account_name}@{domain}"
        escaped_cn = escape_dn_value(cn)
        user_dn = f"CN={escaped_cn},{parent_dn}"
        initial_password = user_data.get("initial_password", "P@ssw0rd2026")

        # 构建用户属性
        attributes = {
            "objectClass": ["top", "person", "organizationalPerson", "user"],
            "cn": cn,
            "sAMAccountName": sam_account_name,
            "userPrincipalName": upn,
            "displayName": cn,
            "userAccountControl": 514,  # 创建时先禁用
        }

        # 可选属性
        if user_data.get("email"):
            attributes["mail"] = user_data["email"]
        if user_data.get("mobile"):
            attributes["mobile"] = user_data["mobile"]
        if user_data.get("title"):
            attributes["title"] = user_data["title"]
        if user_data.get("job_number"):
            attributes["employeeID"] = user_data["job_number"]

        try:
            # 步骤1：创建用户对象（禁用状态）
            result = self.conn.add(user_dn, attributes=attributes)
            if not result:
                error_msg = str(self.conn.result.get("message", "创建失败"))
                logger.error(f"创建AD用户失败: {user_dn}, {error_msg}")
                return False, error_msg

            # 步骤2：设置密码（优先加密LDAP，备选SMB/RPC）
            if not self.connection_secured:
                # 最后一次尝试 StartTLS
                try:
                    if self.conn.start_tls(tls=self.tls_config):
                        self.connection_secured = True
                        logger.info("最后一次StartTLS尝试成功")
                except Exception:
                    pass

            if self.connection_secured:
                # 加密通道可用 → LDAP unicodePwd
                try:
                    encoded_pwd = encode_ad_password(initial_password)
                    pwd_result = self.conn.modify(
                        user_dn,
                        {"unicodePwd": [(MODIFY_REPLACE, [encoded_pwd])]}
                    )
                    if not pwd_result:
                        pwd_error = str(self.conn.result)
                        logger.error(f"LDAP密码设置失败: {user_dn}, {pwd_error}")
                        return False, f"密码设置失败: {pwd_error}"
                    logger.info(f"LDAP密码设置成功: {user_dn}")
                except Exception as e:
                    logger.error(f"LDAP密码设置异常: {user_dn}, {e}")
                    return False, f"密码设置异常: {str(e)}"
            else:
                # 加密不可用 → 走 SMB/RPC 通道
                logger.info(f"LDAP未加密，尝试SMB方式设置密码: {sam_account_name}")
                if not self.set_password_via_smb(sam_account_name, initial_password):
                    logger.error(f"SMB密码设置失败: {sam_account_name}")
                    return False, "密码设置失败：LDAP未加密且SMB方式也失败，请检查 samba-common-bin 是否安装"
                logger.info(f"SMB密码设置成功: {sam_account_name}")

            # 步骤3：启用账号
            try:
                self.conn.modify(
                    user_dn,
                    {"userAccountControl": [(MODIFY_REPLACE, [512])]}
                )
            except Exception as e:
                logger.warning(f"启用账号失败: {user_dn}, {e}")

            logger.info(f"创建AD用户成功: {user_dn}")
            return True, user_dn

        except LDAPException as e:
            logger.error(f"创建AD用户异常: {user_dn}, {e}")
            return False, str(e)

    def modify_user(self, dn: str, changes: dict) -> bool:
        """
        修改用户属性

        Args:
            dn: 用户DN
            changes: 要修改的属性字典 {attr: value}

        Returns:
            是否修改成功
        """
        self._ensure_connection()
        ldap_changes = {}
        for attr, value in changes.items():
            ldap_changes[attr] = [(MODIFY_REPLACE, [value])]

        try:
            result = self.conn.modify(dn, ldap_changes)
            if result:
                logger.info(f"修改AD用户成功: {dn}, 变更: {list(changes.keys())}")
                return True
            else:
                logger.error(f"修改AD用户失败: {dn}, {self.conn.result}")
                return False
        except LDAPException as e:
            logger.error(f"修改AD用户异常: {dn}, {e}")
            return False

    def move_user(self, current_dn: str, new_parent_dn: str) -> bool:
        """
        移动用户到新的OU

        Args:
            current_dn: 当前用户DN
            new_parent_dn: 新的父OU DN

        Returns:
            是否移动成功
        """
        self._ensure_connection()
        # 从当前DN中提取CN部分
        cn_part = current_dn.split(",", 1)[0]
        new_dn = f"{cn_part},{new_parent_dn}"

        try:
            result = self.conn.modify_dn(current_dn, cn_part, new_superior=new_parent_dn)
            if result:
                logger.info(f"移动AD用户成功: {current_dn} → {new_dn}")
                return True
            else:
                logger.error(f"移动AD用户失败: {current_dn}, {self.conn.result}")
                return False
        except LDAPException as e:
            logger.error(f"移动AD用户异常: {current_dn}, {e}")
            return False

    def disable_user(self, dn: str) -> bool:
        """
        禁用用户账号（userAccountControl设为514）

        Args:
            dn: 用户DN

        Returns:
            是否禁用成功
        """
        self._ensure_connection()
        try:
            result = self.conn.modify(
                dn,
                {"userAccountControl": [(MODIFY_REPLACE, [514])]}
            )
            if result:
                logger.info(f"禁用AD用户成功: {dn}")
                return True
            else:
                logger.error(f"禁用AD用户失败: {dn}, {self.conn.result}")
                return False
        except LDAPException as e:
            logger.error(f"禁用AD用户异常: {dn}, {e}")
            return False

    def enable_user(self, dn: str) -> bool:
        """
        启用用户账号（userAccountControl设为512）

        Args:
            dn: 用户DN

        Returns:
            是否启用成功
        """
        self._ensure_connection()
        try:
            result = self.conn.modify(
                dn,
                {"userAccountControl": [(MODIFY_REPLACE, [512])]}
            )
            if result:
                logger.info(f"启用AD用户成功: {dn}")
                return True
            else:
                logger.error(f"启用AD用户失败: {dn}, {self.conn.result}")
                return False
        except LDAPException as e:
            logger.error(f"启用AD用户异常: {dn}, {e}")
            return False

    def delete_user(self, dn: str) -> bool:
        """
        删除AD用户

        Args:
            dn: 用户DN

        Returns:
            是否删除成功
        """
        self._ensure_connection()
        try:
            result = self.conn.delete(dn)
            if result:
                logger.info(f"删除AD用户成功: {dn}")
                return True
            logger.error(f"删除AD用户失败: {dn}, {self.conn.result}")
            return False
        except LDAPException as e:
            logger.error(f"删除AD用户异常: {dn}, {e}")
            return False

    def delete_ou(self, dn: str) -> bool:
        """
        删除AD中的OU（要求OU为空）

        Args:
            dn: OUDN

        Returns:
            是否删除成功
        """
        self._ensure_connection()
        try:
            result = self.conn.delete(dn)
            if result:
                logger.info(f"删除OU成功: {dn}")
                return True
            logger.error(f"删除OU失败: {dn}, {self.conn.result}")
            return False
        except LDAPException as e:
            logger.error(f"删除OU异常: {dn}, {e}")
            return False

    def clear_all_data(self, base_dn: str) -> dict:
        """
        清空AD中由本工具创建的所有数据（用户、OU）

        Args:
            base_dn: AD根OU的DN

        Returns:
            清理结果统计
        """
        self._ensure_connection()
        result = {"deleted_users": 0, "deleted_ous": 0, "failed": 0}

        # 1. 删除 base_dn 下的所有用户
        try:
            self.conn.search(
                search_base=base_dn,
                search_filter="(&(objectClass=user)(objectCategory=person))",
                search_scope=SUBTREE,
                attributes=["distinguishedName"]
            )
            user_dns = [str(entry.distinguishedName) for entry in self.conn.entries]
            for user_dn in user_dns:
                if self.delete_user(user_dn):
                    result["deleted_users"] += 1
                else:
                    result["failed"] += 1
        except LDAPException as e:
            logger.error(f"清空用户失败: {e}")
            result["failed"] += 1

        # 3. 删除 base_dn 下的所有子OU（从深到浅，确保先删叶子）
        try:
            self.conn.search(
                search_base=self.base_dn,
                search_filter="(objectClass=organizationalUnit)",
                search_scope=SUBTREE,
                attributes=["distinguishedName"]
            )
            ou_dns = [str(entry.distinguishedName) for entry in self.conn.entries]
            # 排除 base_dn 本身（如果它也是OU）
            ou_dns = [dn for dn in ou_dns if dn.lower() != self.base_dn.lower()]
            # 按深度从深到浅排序（DN越长层级越深）
            ou_dns.sort(key=lambda x: len(x.split(",")), reverse=True)
            for ou_dn in ou_dns:
                if self.delete_ou(ou_dn):
                    result["deleted_ous"] += 1
                else:
                    result["failed"] += 1
        except LDAPException as e:
            logger.error(f"清空OU失败: {e}")
            result["failed"] += 1

        return result

# ==================== 同步逻辑 ====================

async def preview_sync(dingtalk_client, ad_service: ADSyncService, config: dict, db=None) -> dict:
    """
    预览同步差异（不执行实际操作）

    Args:
        dingtalk_client: 钉钉客户端
        ad_service: AD同步服务
        config: 配置字典
        db: 数据库操作类（可选，用于查询主部门覆盖）

    Returns:
        差异信息字典
    """
    base_dn = config.get("ad_base_dn", "OU=Users,DC=example,DC=com")

    # 1. 获取钉钉部门和用户
    dingtalk_depts = await dingtalk_client.get_departments()
    dingtalk_users = await dingtalk_client.get_all_users()

    # 2. 获取AD现有数据
    ad_service.connect()
    try:
        ad_users = await asyncio.to_thread(ad_service.get_existing_users)
        existing_ous = await asyncio.to_thread(ad_service.get_all_ous)

        # 3. 构建部门映射
        dept_map = {d["dept_id"]: d for d in dingtalk_depts}

        # 4. 计算需要创建的OU
        new_ous = []
        for dept in dingtalk_depts:
            parent_id = dept.get("parent_id", 0)
            if parent_id is None:
                parent_id = 0
            # 跳过根部门（parent_id 为 0 或钉钉标准 dept_id 为 1）
            if parent_id == 0 or dept["dept_id"] == 1:
                continue
            ou_path = get_dept_ou_path(dept["dept_id"], dept_map, base_dn)
            if ou_path not in existing_ous:
                new_ous.append({
                    "name": dept["name"],
                    "ou_path": ou_path,
                    "parent_dept": dept_map.get(dept["parent_id"], {}).get("name", "根部门")
                })

        # 5. 构建用户映射
        ad_user_map = {u["cn"]: u for u in ad_users if u["cn"]}
        dingtalk_user_map = {u["name"]: u for u in dingtalk_users if u["name"]}

        # 6. 计算新增用户
        new_users = []
        for name, dt_user in dingtalk_user_map.items():
            if name not in ad_user_map:
                dept_id_list = dt_user.get("dept_id_list", [])
                dept_id = dept_id_list[0] if dept_id_list else 1
                ou_path = get_dept_ou_path(dept_id, dept_map, base_dn)
                dept_name = dept_map.get(dept_id, {}).get("name", "根部门")
                new_users.append({
                    "name": name,
                    "userid": dt_user.get("userid", ""),
                    "account": dt_user.get("account", ""),
                    "mobile": dt_user.get("mobile", ""),
                    "email": dt_user.get("email", ""),
                    "title": dt_user.get("title", ""),
                    "job_number": dt_user.get("job_number", ""),
                    "department": dept_name,
                    "ou_path": ou_path,
                    "sAMAccountName": generate_sam_account_name(dt_user),
                })

        # 7. 计算属性变更用户
        modified_users = []
        for name, dt_user in dingtalk_user_map.items():
            if name not in ad_user_map:
                continue
            ad_user = ad_user_map[name]
            changes = {}

            if dt_user.get("email") and ad_user.get("mail") != dt_user.get("email"):
                changes["mail"] = dt_user.get("email", "")
            if dt_user.get("mobile") and ad_user.get("mobile") != dt_user.get("mobile"):
                changes["mobile"] = dt_user.get("mobile", "")
            if dt_user.get("title") and ad_user.get("title") != dt_user.get("title"):
                changes["title"] = dt_user.get("title", "")
            if dt_user.get("job_number") and ad_user.get("employeeID") != dt_user.get("job_number"):
                changes["employeeID"] = dt_user.get("job_number", "")
            if ad_user.get("displayName") != name:
                changes["displayName"] = name

            # 检查是否需要移动OU
            dept_id_list = dt_user.get("dept_id_list", [])
            dept_id = dept_id_list[0] if dept_id_list else 1
            expected_ou = get_dept_ou_path(dept_id, dept_map, base_dn)
            current_dn = ad_user.get("dn", "")
            needs_move = False
            if current_dn and expected_ou and not current_dn.lower().endswith(expected_ou.lower()):
                needs_move = True
                changes["move_to"] = expected_ou

            if changes:
                modified_users.append({
                    "name": name,
                    "dn": current_dn,
                    "department": dept_map.get(dept_id, {}).get("name", "根部门"),
                    "changes": changes,
                    "needs_move": needs_move,
                })

        # 8. 计算需要禁用的用户
        # 安全保护：防止钉钉API异常返回空数据时误判全量离职
        disabled_users = []
        dd_count = len(dingtalk_users)
        ad_count = len(ad_users)
        skip_disable_preview = False

        if dd_count == 0:
            skip_disable_preview = True
            logger.warning(f"预览：钉钉API返回0个用户，疑似API异常，禁用预览不可信")
        elif ad_count > 0 and dd_count < ad_count * 0.3:
            skip_disable_preview = True
            logger.warning(f"预览：钉钉用户数({dd_count})仅为AD用户数({ad_count})的{dd_count*100//ad_count}%，禁用预览不可信")

        if not skip_disable_preview:
            for name, ad_user in ad_user_map.items():
                if name not in dingtalk_user_map:
                    uac = ad_user.get("userAccountControl", 512)
                    if uac != 514:  # 未禁用
                        disabled_users.append({
                            "name": name,
                            "dn": ad_user.get("dn", ""),
                            "current_uac": uac,
                            "sAMAccountName": ad_user.get("sAMAccountName", ""),
                        })

    finally:
        ad_service.disconnect()

    return {
        "new_ous": new_ous,
        "new_users": new_users,
        "modified_users": modified_users,
        "disabled_users": disabled_users,
        "dingtalk_user_count": len(dingtalk_users),
        "ad_user_count": len(ad_users),
        "dingtalk_dept_count": len(dingtalk_depts),
    }

async def execute_sync(dingtalk_client, ad_service: ADSyncService, config: dict, db) -> dict:
    """
    执行完整同步流程

    流程：
    1. 从钉钉拉取所有部门和用户
    2. 从AD获取现有用户和OU列表
    3. 创建缺失的OU
    4. 创建/修改/移动用户
    5. 禁用AD中多余的账号
    6. 记录同步日志

    Args:
        dingtalk_client: 钉钉客户端
        ad_service: AD同步服务
        config: 配置字典
        db: 数据库操作类

    Returns:
        同步结果字典
    """
    base_dn = config.get("ad_base_dn", "OU=Users,DC=example,DC=com")
    initial_password = config.get("initial_password", "P@ssw0rd2026")

    # 生成本次同步的唯一批次ID
    sync_batch_id = str(uuid4())

    total = 0
    success_count = 0
    failed_count = 0

    # 批次操作统计（用于 summary 日志）
    batch_stats: dict = {}

    def _track(op_type: str, op_status: str):
        """记录批次操作统计"""
        if op_type not in batch_stats:
            batch_stats[op_type] = {"success": 0, "failed": 0, "skipped": 0}
        batch_stats[op_type][op_status] = batch_stats[op_type].get(op_status, 0) + 1

    await db.update_sync_status(is_running=True, last_sync_status="running")

    try:
        # 1. 获取钉钉数据
        dingtalk_depts = await dingtalk_client.get_departments()
        dingtalk_users = await dingtalk_client.get_all_users()

        # 2. 连接AD并获取数据
        ad_service.connect()
        ad_users = await asyncio.to_thread(ad_service.get_existing_users)
        existing_ous = await asyncio.to_thread(ad_service.get_all_ous)

        # 3. 构建部门映射
        dept_map = {d["dept_id"]: d for d in dingtalk_depts}

        # 4. 创建缺失的OU（按层级从浅到深排序，确保父OU先创建）
        sorted_depts = sorted(dingtalk_depts, key=lambda d: get_dept_depth(d["dept_id"], dept_map))
        failed_parent_ids = set()  # 记录父OU创建失败的dept_id，跳过其子部门
        for dept in sorted_depts:
            parent_id = dept.get("parent_id", 0)
            if parent_id is None:
                parent_id = 0
            # 跳过根部门（parent_id 为 0 或钉钉标准 dept_id 为 1）
            if parent_id == 0 or dept["dept_id"] == 1:
                continue
            # 跳过父OU创建失败的子部门（避免链式失败）
            if parent_id in failed_parent_ids:
                continue
            ou_path = get_dept_ou_path(dept["dept_id"], dept_map, base_dn)
            if ou_path not in existing_ous:
                parent_dn = get_dept_ou_path(dept["parent_id"], dept_map, base_dn)
                total += 1
                try:
                    result = await asyncio.to_thread(ad_service.create_ou, dept["name"], parent_dn)
                    if result:
                        success_count += 1
                        _track("create_ou", "success")
                        existing_ous.append(ou_path)
                        await db.add_log(
                            operation_type="create_ou",
                            target_dn=ou_path,
                            target_name=dept["name"],
                            status="success",
                            detail=json.dumps({"ou_name": dept["name"], "parent": parent_dn}, ensure_ascii=False),
                            sync_batch_id=sync_batch_id
                        )
                    else:
                        failed_count += 1
                        _track("create_ou", "failed")
                        failed_parent_ids.add(dept["dept_id"])
                        await db.add_log(
                            operation_type="create_ou",
                            target_dn=ou_path,
                            target_name=dept["name"],
                            status="failed",
                            error_message=f"创建OU返回失败 (parent={parent_dn})",
                            sync_batch_id=sync_batch_id
                        )
                except Exception as e:
                    failed_count += 1
                    _track("create_ou", "failed")
                    failed_parent_ids.add(dept["dept_id"])
                    await db.add_log(
                        operation_type="create_ou",
                        target_dn=ou_path,
                        target_name=dept["name"],
                        status="failed",
                        error_message=str(e),
                        sync_batch_id=sync_batch_id
                    )

        # 5. 构建用户映射
        ad_user_map = {u["cn"]: u for u in ad_users if u["cn"]}
        dingtalk_user_map = {u["name"]: u for u in dingtalk_users if u["name"]}

        # 6. 创建新用户
        for name, dt_user in dingtalk_user_map.items():
            if name in ad_user_map:
                continue

            dept_id_list = dt_user.get("dept_id_list", [])
            dept_id = dept_id_list[0] if dept_id_list else 1
            ou_path = get_dept_ou_path(dept_id, dept_map, base_dn)

            user_data = {
                "name": name,
                "userid": dt_user.get("userid", ""),
                "account": dt_user.get("account", ""),
                "mobile": dt_user.get("mobile", ""),
                "email": dt_user.get("email", ""),
                "title": dt_user.get("title", ""),
                "job_number": dt_user.get("job_number", ""),
                "initial_password": initial_password,
            }

            total += 1
            try:
                result, dn_or_error = await asyncio.to_thread(
                    ad_service.create_user, user_data, ou_path
                )
                if result:
                    success_count += 1
                    _track("create_user", "success")
                    await db.add_log(
                        operation_type="create_user",
                        target_dn=dn_or_error,
                        target_name=name,
                        status="success",
                        detail=json.dumps({"userid": dt_user.get("userid", ""), "department": dept_map.get(dept_id, {}).get("name", "")}, ensure_ascii=False),
                        sync_batch_id=sync_batch_id
                    )
                else:
                    failed_count += 1
                    _track("create_user", "failed")
                    await db.add_log(
                        operation_type="create_user",
                        target_dn=f"CN={name},{ou_path}",
                        target_name=name,
                        status="failed",
                        error_message=dn_or_error,
                        sync_batch_id=sync_batch_id
                    )
            except Exception as e:
                failed_count += 1
                _track("create_user", "failed")
                await db.add_log(
                    operation_type="create_user",
                    target_dn=f"CN={name},{ou_path}",
                    target_name=name,
                    status="failed",
                    error_message=str(e),
                    sync_batch_id=sync_batch_id
                )

        # 7. 修改和移动已有用户
        for name, dt_user in dingtalk_user_map.items():
            if name not in ad_user_map:
                continue

            ad_user = ad_user_map[name]
            current_dn = ad_user.get("dn", "")

            # 检查OU是否需要移动
            dept_id_list = dt_user.get("dept_id_list", [])
            dept_id = dept_id_list[0] if dept_id_list else 1
            expected_ou = get_dept_ou_path(dept_id, dept_map, base_dn)

            actual_dn = current_dn
            needs_move = False
            if current_dn and expected_ou and not current_dn.lower().endswith(expected_ou.lower()):
                needs_move = True

            # 执行移动
            if needs_move:
                total += 1
                try:
                    result = await asyncio.to_thread(ad_service.move_user, current_dn, expected_ou)
                    if result:
                        success_count += 1
                        _track("move_user", "success")
                        actual_dn = f"CN={escape_dn_value(name)},{expected_ou}"
                        await db.add_log(
                            operation_type="move_user",
                            target_dn=current_dn,
                            target_name=name,
                            status="success",
                            detail=json.dumps({"from": current_dn, "to": actual_dn}, ensure_ascii=False),
                            sync_batch_id=sync_batch_id
                        )
                    else:
                        failed_count += 1
                        _track("move_user", "failed")
                        await db.add_log(
                            operation_type="move_user",
                            target_dn=current_dn,
                            target_name=name,
                            status="failed",
                            error_message="移动用户失败",
                            sync_batch_id=sync_batch_id
                        )
                except Exception as e:
                    failed_count += 1
                    _track("move_user", "failed")
                    await db.add_log(
                        operation_type="move_user",
                        target_dn=current_dn,
                        target_name=name,
                        status="failed",
                        error_message=str(e),
                        sync_batch_id=sync_batch_id
                    )

            # 检查属性变更
            changes = {}
            if dt_user.get("email") and ad_user.get("mail") != dt_user.get("email"):
                changes["mail"] = dt_user.get("email", "")
            if dt_user.get("mobile") and ad_user.get("mobile") != dt_user.get("mobile"):
                changes["mobile"] = dt_user.get("mobile", "")
            if dt_user.get("title") and ad_user.get("title") != dt_user.get("title"):
                changes["title"] = dt_user.get("title", "")
            if dt_user.get("job_number") and ad_user.get("employeeID") != dt_user.get("job_number"):
                changes["employeeID"] = dt_user.get("job_number", "")
            if ad_user.get("displayName") != name:
                changes["displayName"] = name

            if changes:
                total += 1
                try:
                    result = await asyncio.to_thread(ad_service.modify_user, actual_dn, changes)
                    if result:
                        success_count += 1
                        _track("modify_user", "success")
                        await db.add_log(
                            operation_type="modify_user",
                            target_dn=actual_dn,
                            target_name=name,
                            status="success",
                            detail=json.dumps(changes, ensure_ascii=False),
                            sync_batch_id=sync_batch_id
                        )
                    else:
                        failed_count += 1
                        _track("modify_user", "failed")
                        await db.add_log(
                            operation_type="modify_user",
                            target_dn=actual_dn,
                            target_name=name,
                            status="failed",
                            error_message="修改属性失败",
                            sync_batch_id=sync_batch_id
                        )
                except Exception as e:
                    failed_count += 1
                    _track("modify_user", "failed")
                    await db.add_log(
                        operation_type="modify_user",
                        target_dn=actual_dn,
                        target_name=name,
                        status="failed",
                        error_message=str(e),
                        sync_batch_id=sync_batch_id
                    )

        # 8. 禁用AD中多余的账号（钉钉中已不存在的用户）
        # 安全保护：防止钉钉API异常返回空数据时全量误禁用
        dd_count = len(dingtalk_users)
        ad_count = len(ad_users)
        skip_disable = False
        disable_reason = ""

        if dd_count == 0:
            skip_disable = True
            disable_reason = f"钉钉API返回0个用户，疑似API异常，跳过禁用步骤以保护AD数据（AD现有{ad_count}个用户）"
            logger.warning(disable_reason)
        elif ad_count > 0 and dd_count < ad_count * 0.3:
            ratio = dd_count * 100 // ad_count
            skip_disable = True
            disable_reason = f"钉钉用户数({dd_count})仅为AD用户数({ad_count})的{ratio}%，疑似API异常，跳过禁用步骤"
            logger.warning(disable_reason)

        if not skip_disable:
            for name, ad_user in ad_user_map.items():
                if name in dingtalk_user_map:
                    continue

                uac = ad_user.get("userAccountControl", 512)
                if uac == 514:
                    continue  # 已经禁用

                total += 1
                try:
                    result = await asyncio.to_thread(ad_service.disable_user, ad_user["dn"])
                    if result:
                        success_count += 1
                        _track("disable_user", "success")
                        await db.add_log(
                            operation_type="disable_user",
                            target_dn=ad_user["dn"],
                            target_name=name,
                            status="success",
                            detail=json.dumps({"reason": "钉钉中不存在该用户"}, ensure_ascii=False),
                            sync_batch_id=sync_batch_id
                        )
                    else:
                        failed_count += 1
                        _track("disable_user", "failed")
                        await db.add_log(
                            operation_type="disable_user",
                            target_dn=ad_user["dn"],
                            target_name=name,
                            status="failed",
                            error_message="禁用用户失败",
                            sync_batch_id=sync_batch_id
                        )
                except Exception as e:
                    failed_count += 1
                    _track("disable_user", "failed")
                    await db.add_log(
                        operation_type="disable_user",
                        target_dn=ad_user["dn"],
                        target_name=name,
                        status="failed",
                        error_message=str(e),
                        sync_batch_id=sync_batch_id
                    )
        else:
            _track("disable_user", "skipped")
            await db.add_log(
                operation_type="disable_user",
                target_dn="N/A",
                target_name="[安全保护]",
                status="skipped",
                detail=json.dumps({"reason": disable_reason}, ensure_ascii=False),
                sync_batch_id=sync_batch_id
            )

        # 9. 更新同步状态
        final_status = "success" if failed_count == 0 else "partial"
        await db.update_sync_status(
            is_running=False,
            last_sync_time=datetime.now().isoformat(),
            last_sync_status=final_status,
            last_sync_total=total,
            last_sync_success=success_count,
            last_sync_failed=failed_count
        )

        # 10. 记录批次摘要日志
        if skip_disable and total == 0:
            summary_status = "skipped"
        elif failed_count == 0:
            summary_status = "success"
        else:
            summary_status = "partial"

        await db.add_log(
            operation_type="sync_summary",
            status=summary_status,
            detail=json.dumps(batch_stats, ensure_ascii=False),
            sync_batch_id=sync_batch_id
        )

        logger.info(f"同步完成: 总计{total}, 成功{success_count}, 失败{failed_count}")

        return {
            "total": total,
            "success": success_count,
            "failed": failed_count,
            "status": final_status,
            "sync_batch_id": sync_batch_id,
        }

    except Exception as e:
        logger.error(f"同步过程发生异常: {e}")
        await db.update_sync_status(
            is_running=False,
            last_sync_time=datetime.now().isoformat(),
            last_sync_status="failed",
            last_sync_total=total,
            last_sync_success=success_count,
            last_sync_failed=failed_count
        )
        # 记录批次摘要日志（异常终止）
        await db.add_log(
            operation_type="sync_summary",
            status="failed",
            detail=json.dumps(batch_stats, ensure_ascii=False),
            sync_batch_id=sync_batch_id
        )
        raise
    finally:
        ad_service.disconnect()
