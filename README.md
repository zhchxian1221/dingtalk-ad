# DingTalk → AD Sync

<p align="center">
  <strong>钉钉组织架构单向同步至 Active Directory 域控</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-green.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3.x-brightgreen.svg" alt="Vue">
  <img src="https://img.shields.io/badge/Docker-✔-2496ED.svg" alt="Docker">
  <img src="https://img.shields.io/badge/QA-204%20Pass-success.svg" alt="QA">
</p>

---

## 目录

- [项目简介](#项目简介)
- [架构总览](#架构总览)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [同步方案详解](#同步方案详解)
- [字段映射](#字段映射)
- [API 接口列表](#api-接口列表)
- [运维指南](#运维指南)
- [FAQ](#faq)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [License](#license)

---

## 项目简介

将**钉钉组织架构**（部门 + 用户）单向同步到 **Microsoft Active Directory** 域控的 Web 管理工具。

Docker 一键部署，浏览器操作，无需编写代码。支持定时自动同步、差异预览、操作日志追溯。

### 为什么不用现有方案？

| 方案 | 问题 |
|------|------|
| 阿里云 IDaaS | 免费版仅 50 用户，500+ 用户需企业版 |
| Simple-IDaaS | 项目已停更，不支持 Docker |
| MaxKey 社区版 | AD 连接器推送仅企业版支持 |

**最终选择自研**：完全可控、无用户数限制、一次部署长期使用。

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                        DingTalk Cloud                            │
│                   (钉钉开放平台 API)                              │
└──────────────────────┬───────────────────────────────────────────┘
                       │ HTTPS
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Docker Container                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    FastAPI Backend                        │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  │   │
│  │  │ DingTalk │  │   Sync   │  │ Scheduler│  │Database │  │   │
│  │  │   API    │  │  Engine  │  │ (APS)    │  │(SQLite) │  │   │
│  │  └──────────┘  └────┬─────┘  └──────────┘  └─────────┘  │   │
│  │                      │                                    │   │
│  └──────────────────────┼────────────────────────────────────┘   │
│                         │ LDAP (389) + SMB (445)                 │
│  ┌──────────────────────┼────────────────────────────────────┐   │
│  │        Vue3 + ElementPlus Frontend (CDN, no build)        │   │
│  └──────────────────────┼────────────────────────────────────┘   │
└─────────────────────────┼────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│              Active Directory Domain Controller                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                       │
│  │    OU    │  │  Users   │  │ Security │                       │
│  │  Trees   │  │ Accounts │  │  Groups  │                       │
│  └──────────┘  └──────────┘  └──────────┘                       │
└──────────────────────────────────────────────────────────────────┘
```

### 同步流程

```
钉钉部门/用户 → 差异对比 → 预览 → 手动/定时触发
                              │
                              ▼
              ┌───────────────────────────────┐
              │  1. 同步 OU（部门 → AD OU）    │
              │  2. 创建/更新用户              │
              │  3. 设置初始密码              │
              │  4. 启用用户                  │
              │  5. 创建安全组（多部门用户）    │
              │  6. 将用户加入安全组           │
              │  7. 禁用离职用户              │
              └───────────────────────────────┘
```

---

## 功能特性

| 模块 | 功能 |
|------|------|
| 📊 **仪表盘** | 钉钉/AD 用户数对比、上次同步时间、差异统计、快捷同步 |
| 🔄 **同步管理** | 差异预览（新增/修改/禁用分类）、执行同步、实时进度 |
| 📋 **操作日志** | 操作历史、按时间/类型/状态筛选、分页浏览、APM追踪 |
| ⚙️ **系统设置** | 钉钉 API、AD 连接、加密状态检测、同步策略、Cron 定时 |

### 核心能力

- **部门映射**：钉钉部门层级 → AD OU 层级，支持嵌套（无深度限制）
- **智能匹配**：以 `cn/displayName` 为主键，钉钉上姓名变更自动更新 AD
- **sAMAccountName**：多策略生成（邮箱前缀 → 拼音 → userid → hash 兜底）
- **密码双通道**：优先 LDAPS / StartTLS 加密设密码，不可用时自动降级 SMB/RPC
- **多部门支持**：用户属多个钉钉部门时，自动创建安全组 `SG_<部门名>` 并加入
- **离职处理**：钉钉中已删除用户 → AD 中禁用账号（不删除，保留审计）
- **定时同步**：Cron 表达式配置，支持每 N 分钟、每天、每周等
- **容器自恢复**：Docker `restart: unless-stopped`，服务器重启后自动拉起

---

## 快速开始

### 前置条件

- Docker 20.10+ / Docker Compose v2
- AD 域控可访问（389/445 端口）
- 钉钉开放平台企业内部应用（AppKey + AppSecret）

### 1. 克隆项目

```bash
git clone https://github.com/zhchxian1221/dingtalk-ad-sync.git
cd dingtalk-ad-sync
```

### 2. 构建并启动

```bash
docker-compose up -d --build
```

首次构建约 2-3 分钟（含依赖下载）。

### 3. 访问 Web 界面

浏览器打开 `http://服务器IP:8080`

### 4. 配置系统

进入「系统设置」页面，依次填写：

#### 钉钉配置

| 字段 | 说明 | 示例 |
|------|------|------|
| AppKey | 钉钉应用 AppKey | `dingxxxxxxxxxxxx` |
| AppSecret | 钉钉应用 AppSecret | `xxxxxxxxxxxxxxxxxxxx` |

#### AD 配置

| 字段 | 说明 | 示例 |
|------|------|------|
| 服务器地址 | AD 域控 IP | `172.16.100.250` |
| 端口 | LDAP 端口 | `389` |
| 管理员账号 | 有 OU 写入权限的 AD 账号 | `administrator@your-domain.com` |
| 密码 | 管理员密码 | `********` |
| Base DN | 最外层 OU 路径 | `OU=Users,DC=example,DC=com` |

#### 同步策略

| 字段 | 说明 | 默认值 |
|------|------|--------|
| 初始密码 | 新用户默认密码 | `P@ssw0rd2026` |
| 多部门安全组 | 多部门用户是否加入安全组 | 开启 |
| 禁用而非删除 | 离职用户处理方式 | 开启 |
| 定时 Cron | 自动同步表达式 | `*/30 * * * *` |

### 5. 测试连接

点击「测试钉钉连接」和「测试 AD 连接」，确认均为**绿色成功**。

> ⚠️ AD 连接测试显示"加密连接"最佳。如显示"非加密"，系统会自动切换 SMB/RPC 模式设置密码（不影响同步功能）。

### 6. 首次同步

进入「同步管理」→「刷新差异预览」→ 确认差异数据 →「执行同步」

---

## 配置说明

### AD 连接加密状态

| 提示 | 含义 | 密码设置方式 |
|------|------|-------------|
| 🟢 加密连接 | LDAPS 或 StartTLS 协商成功 | LDAP unicodePwd |
| 🟡 非加密 | 域控无 TLS 证书 | SMB/RPC (`net rpc password`) |

两种方式均能正常设置密码，功能无差别。

### Cron 表达式示例

| 表达式 | 含义 |
|--------|------|
| `*/30 * * * *` | 每 30 分钟 |
| `0 */2 * * *` | 每 2 小时 |
| `0 8 * * 1-5` | 工作日 8:00 |
| `0 2 * * *` | 每天凌晨 2:00 |

### 安全组命名规则

```
钉钉部门             AD 安全组
─────────          ─────────
研发部          →  SG_yanfabu           (中文→拼音)
销售部          →  SG_xiaoshoubu
Market部        →  SG_Market            (保留英文)
```

- `cn` 显示名保留中文：`SG_研发部`
- `sAMAccountName` 自动转为 ASCII 拼音并截断至 20 字符

---

## 同步方案详解

### OU 映射规则

```
钉钉组织树                         AD OU 树
───────────                      ──────────
公司根部门              ───→      OU=根部门,OU=Users,DC=...
├── 研发部              ───→      ├── OU=研发部
│   ├── 前端组          ───→      │   └── OU=前端组
│   └── 后端组          ───→      │       └── OU=后端组
├── 销售部              ───→      ├── OU=销售部
└── 行政部              ───→      └── OU=行政部
```

- 根部门直接挂在 Base DN 下
- 子部门按层级依次嵌套
- 父 OU 创建失败 → 自动跳过所有子部门（避免链式失败）

### sAMAccountName 生成优先级

```
1. 邮箱前缀  (user@corp.com → user)
2. account 字段（含中文则先转拼音）
3. 姓名转拼音
4. userid
5. hash 兜底
```

### 多部门用户处理

```
用户 "张三" 属于 [研发部, 项目部]
                │
                ▼
主部门=研发部 → OU=OU=研发部,...
其他部门      → 加入 SG_项目部 安全组
```

- **单部门用户**：仅放入对应 OU，不创建安全组
- **多部门用户**：主部门决定 OU 位置，所有部门对应安全组

---

## 字段映射

| 钉钉字段 | AD 属性 | 说明 |
|----------|---------|------|
| `name` | `cn` / `displayName` | **唯一匹配键** |
| sAMAccountName (生成) | `sAMAccountName` | 登录名，按优先级策略生成 |
| `mobile` | `mobile` | 手机号 |
| `email` | `mail` | 邮箱 |
| `title` | `title` | 职位 |
| `job_number` | `employeeID` | 工号 |
| (固定值) | `userAccountControl` | `512` 正常 / `514` 禁用 |

---

## API 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/config` | 获取系统配置 |
| `PUT` | `/api/config` | 更新系统配置 |
| `GET` | `/api/sync/status` | 同步状态 |
| `GET` | `/api/sync/preview` | 差异预览 |
| `POST` | `/api/sync/execute` | 执行同步 |
| `GET` | `/api/logs` | 查询操作日志 |
| `POST` | `/api/ad/test` | 测试 AD 连接 |
| `POST` | `/api/dingtalk/test` | 测试钉钉连接 |
| `PUT` | `/api/scheduler` | 更新定时任务 |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/app` | 前端 SPA |

---

## 运维指南

### 启动与停止

```bash
# 启动
docker-compose up -d

# 首次构建启动
docker-compose up -d --build

# 停止
docker-compose down

# 重启
docker-compose restart

# 查看实时日志
docker-compose logs -f
```

### 开机自启

容器已配置 `restart: unless-stopped`。确认 Docker 本身开机自启：

```bash
systemctl enable docker
systemctl is-enabled docker  # 应输出 enabled
```

### 数据备份

```bash
# 备份数据库（同步记录 + 配置）
cp ./data/sync.db ./backup/sync_$(date +%Y%m%d).db

# 备份整个项目
tar -czf dingtalk-ad-sync-backup.tar.gz ./data/ ./logs/
```

### 更新代码

```bash
git pull
docker-compose down && docker-compose up -d --build
```

### 监控 & 调试

```bash
# 容器状态
docker ps -a | grep dingtalk

# 进入容器
docker exec -it dingtalk-ad-sync bash

# 查看资源占用
docker stats dingtalk-ad-sync
```

---

## FAQ

<details>
<summary><strong>Q: AD 连接测试显示"非加密"，影响使用吗？</strong></summary>

不影响。系统会自动走 SMB/RPC 通道设置密码，功能与加密 LDAP 完全一致。
</details>

<details>
<summary><strong>Q: 同步后用户全是禁用状态？</strong></summary>

说明密码未设置成功。检查：AD 连接测试是否成功 → 确认 `samba-common-bin` 已安装 → 查看容器日志。
</details>

<details>
<summary><strong>Q: 安全组是空的？</strong></summary>

单部门用户不会加入安全组（OU 本身已确定部门归属）。仅多部门用户会加入所有部门对应的安全组。
</details>

<details>
<summary><strong>Q: 安全组创建失败？</strong></summary>

可能原因：`sAMAccountName` 含中文/超长。系统已自动转拼音截断；如仍有问题，检查 AD 权限。
</details>

<details>
<summary><strong>Q: Docker 构建卡在 apt-get？</strong></summary>

已配置清华镜像源。如网络环境特殊，可进一步修改 Dockerfile 中的 `sed` 行换为内部镜像。
</details>

<details>
<summary><strong>Q: 关闭浏览器后同步还在跑吗？</strong></summary>

同步在后端执行，关浏览器不影响。定时任务也在后端运行，完全不依赖浏览器。
</details>

<details>
<summary><strong>Q: 域控没有 TLS 证书怎么办？</strong></summary>

系统自动检测并降级到 SMB/RPC 通道（走 445 端口 SAMR 命名管道），不依赖 LDAP 加密。确保 445 端口可达即可。
</details>

---

## 技术栈

| 层级 | 技术 | 版本 |
|------|------|------|
| 后端框架 | FastAPI | 0.115 |
| ASGI 服务器 | Uvicorn | 0.30 |
| LDAP 客户端 | ldap3 | 2.9 |
| HTTP 客户端 | httpx | 0.27 |
| 定时任务 | APScheduler | 3.10 |
| 数据库 | SQLite (aiosqlite) | 0.20 |
| 中文拼音 | pypinyin | 0.50 |
| 前端框架 | Vue 3 (CDN) | 3.x |
| UI 组件库 | Element Plus (CDN) | 2.x |
| 容器化 | Docker + Compose | - |
| 辅助工具 | samba-common-bin | - |

---

## 项目结构

```
dingtalk-ad-sync/
├── backend/
│   ├── main.py              # FastAPI 应用入口 + 全部 API 路由
│   ├── ad_sync.py            # AD 同步引擎（OU/用户/安全组 CRUD）
│   ├── database.py           # SQLite 数据库操作层
│   ├── dingtalk_api.py       # 钉钉开放平台 API 封装
│   ├── scheduler.py          # APScheduler 定时任务
│   ├── requirements.txt      # Python 依赖清单
│   └── test_pinyin.py        # 拼音转换单元测试
├── frontend/
│   └── index.html            # Vue3 + ElementPlus 单页应用
├── tests/
│   ├── conftest.py           # pytest 配置
│   ├── test_ad_sync.py       # AD 同步引擎测试
│   ├── test_database.py      # 数据库层测试
│   ├── test_dingtalk_api.py  # 钉钉 API 测试
│   ├── test_main.py          # API 路由测试
│   └── test_scheduler.py     # 定时任务测试
├── data/                     # SQLite 数据持久化卷
├── Dockerfile
├── docker-compose.yml
├── pytest.ini
├── LICENSE
└── README.md
```

---

## License

MIT License

---
