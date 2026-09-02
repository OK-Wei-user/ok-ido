# I-DO - 通用 AI Agent 系统

<p align="center">
  <strong>私有化部署 | A2A + MCP 协议 | 沙箱隔离执行 | 多模态能力 | 建议使用**Trae CN**一键部署</strong>
</p>

---

## 目录

- [这是什么？](#这是什么)
- [能力演示](#能力演示)
- [系统架构](#系统架构)
- [一、项目完整配置清单](#一项目完整配置清单)
- [二、本地部署前置环境要求 + 分步部署实现方案](#二本地部署前置环境要求--分步部署实现方案)
- [三、线上服务端部署硬件 / 软件条件 + 完整部署实施步骤](#三线上服务端部署硬件--软件条件--完整部署实施步骤)
- [附加：100 人并发承载力评估与优化方向](#附加100-人并发承载力评估与优化方向)
- [常用运维命令](#常用运维命令)
- [常见问题](#常见问题)
- [相关文档](#相关文档)

---

## 这是什么？

**I-DO**是一个通用 AI 智能体（Agent）系统，定位为"AI 员工"：

- **理解需求**：用自然语言描述任务目标
- **自主规划与执行**：自动拆解任务、调用工具、逐步完成
- **操作电脑**：在隔离沙箱中执行代码、操作浏览器、处理文件
- **连接外部服务**：通过 MCP 协议接入外部工具，通过 A2A 协议调用其他 Agent
- **完全私有化**：所有数据和服务运行在自有服务器上

> 核心能力：信息搜集、数据分析、文档撰写、浏览器自动化、代码执行，全程 SSE 流式输出。

---

## 能力演示

> 📎 完整能力介绍文档（点击下载）：[I-DO智能办公伙伴介绍.pptx](example-image/I-DO智能办公伙伴介绍.pptx)

**整体执行流程（Planner 规划 → ReAct 执行 → SSE 流式输出）：**

![I-DO 执行流程图](example-image/流程图.png)

**多模态能力（图像理解 / OCR / 语音识别 / 图像生成 / 视频分析）：**

![多模态能力](example-image/多模态.png)

**PPT 生成能力（文档撰写 / PPT 自动生成 / 文件处理）：**

![能力介绍-PPT生成](example-image/能力介绍-PPT生成.png)

**系统集成与数据分析能力（MCP / A2A / 代码执行 / 数据处理）：**

![系统集成及数据分析能力](example-image/系统集成及数据分析能力.png)

---

## 系统架构

```
                          用户浏览器
                              │
                              ▼
                    ┌─────────────────┐
                    │     Nginx       │  ← 统一入口（反向代理，仅暴露 80/443）
                    │   (Port 80/443) │
                    └────────┬────────┘
                             │
               ┌─────────────┴─────────────┐
               │ /                         │ /api
               ▼                           ▼
        ┌─────────────┐             ┌──────────────┐
        │  Next.js UI │             │   FastAPI     │  ← 核心中控
        │ (Port 3000) │             │ (Port 8000)   │
        └─────────────┘             └──────┬───────┘
                                          │
               ┌──────────────────────────┼──────────────────────────┐
               │                          │                          │
               ▼                          ▼                          ▼
        ┌─────────────┐          ┌──────────────┐          ┌─────────────────┐
        │ PostgreSQL  │          │    Redis     │          │  Sandbox 容器   │
        │ (Port 5432) │          │ (Port 6379)  │          │ (动态创建/销毁)  │
        │  持久化存储   │          │  消息队列/缓存 │          │ 代码执行+浏览器  │
        └─────────────┘          └──────────────┘          └─────────────────┘
                                                                  │
                                          ┌───────────────────────┼───────────────────────┐
                                          │                       │                       │
                                          ▼                       ▼                       ▼
                                   ┌────────────┐         ┌────────────┐          ┌────────────┐
                                   │ MCP 多模态  │         │  SearXNG   │          │  A2A Agent │
                                   │ (Port 9100) │         │ (Port 8080) │          │  (外部服务) │
                                   │ 图像/语音/OCR│         │  元搜索引擎  │          │  Agent互联  │
                                   └────────────┘         └────────────┘          └────────────┘
```

### 数据流

1. 用户在浏览器输入需求 → Nginx 转发至后端 API
2. **PlannerAgent**（规划者）分析需求，生成分步执行计划
3. **ReActAgent**（执行者）按计划逐步调用工具完成任务
4. 工具执行发生在隔离的 **沙箱容器**（代码运行、浏览器操作）
5. 结果通过 SSE（Server-Sent Events）实时流式返回前端

### 项目结构

```
i_do/
├── api/                        # 后端 API 服务（Python 3.12 + FastAPI）
│   ├── app/
│   │   ├── application/        #   应用层：业务服务编排（agent/session/user/file/status）
│   │   ├── domain/             #   领域层：Agent、工具、流程核心逻辑
│   │   │   ├── models/         #     领域模型（event/session/user/file/plan/research...）
│   │   │   ├── services/       #     核心服务（agent_task_runner/flows/agents/tools/skills）
│   │   │   └── repositories/   #     仓储接口（uow/session/user/file）
│   │   ├── infrastructure/     #   基础设施层：数据库、Redis、COS、沙箱、LLM、搜索引擎
│   │   └── interfaces/         #   接口层：API 路由、Schema、异常处理、中间件
│   ├── core/config.py          #   配置管理（.env 加载，Pydantic Settings）
│   ├── config.yaml             #   应用配置（LLM / MCP / A2A / 搜索 / 深度研究）
│   ├── alembic/                #   数据库迁移脚本（5 个版本）
│   ├── skills/                 #   内置技能（docx / pdf / pptx / xlsx / summarize...）
│   └── tests/                  #   单元测试
│
├── ui/                         # 前端 UI 服务（Next.js 16 + React 19 + pnpm）
│   ├── src/
│   │   ├── app/                #   页面路由（首页、会话、登录、注册）
│   │   ├── components/         #   UI 组件（聊天、工具展示、VNC 远程桌面）
│   │   ├── lib/api/            #   API 客户端封装（fetch + SSE + Token 自动刷新）
│   │   └── providers/          #   Context Providers（认证、会话）
│   ├── next.config.ts          #   output: "standalone"（生产精简产物）
│   ├── Dockerfile              #   生产镜像（corepack + pnpm + standalone 三阶段）
│   └── pnpm-workspace.yaml     #   pnpm 工作区配置
│
├── sandbox/                    # 沙箱服务（Ubuntu 22.04 + Chromium + VNC）
│   ├── app/                    #   FastAPI 服务（文件操作、Shell 执行）
│   ├── supervisord.conf        #   进程管理（Chrome、VNC、API）
│   ├── requirements.txt        #   预装 Python 库（pandas/numpy/openpyxl/matplotlib/opencv/rembg...）
│   └── Dockerfile              #   含 LibreOffice + poppler + qpdf + pandoc
│
├── mcp-multimodal/             # MCP 多模态服务（图像理解、OCR、语音、视频）
│   ├── mcp_multimodal/
│   │   ├── server.py           #   MCP 服务入口（streamable-http，/mcp 端点）
│   │   ├── tools/              #   8 个多模态工具
│   │   ├── client.py           #   BigModel API 客户端
│   │   └── utils/              #   截图/文件/URL 工具
│   └── Dockerfile              #   多阶段构建（builder + Playwright Chromium runtime）
│
├── searxng/                    # SearXNG 元搜索引擎配置
│   └── settings.yml            #   引擎：Google/Bing/DDG/Wikipedia/Brave，语言 zh-CN
│
├── nginx/                      # Nginx 网关配置
│   ├── nginx.conf              #   主配置（gzip / WebSocket map / 100m 上传限制）
│   └── conf.d/default.conf     #   站点配置（API 代理 + 前端代理 + SSE/WebSocket）
│
├── docker-compose.yml          # 本地部署编排（后端基础设施 + API，不含 UI/Nginx）
├── docker-compose.prod.yml     # 生产部署编排（全栈：含 UI + Nginx，含资源限制）
├── deploy.ps1                  # Windows 一键本地部署脚本
├── .env                        # 环境变量配置
└── README.md
```

---

## 一、项目完整配置清单

项目配置分三层：**环境变量（.env）**、**应用配置（api/config.yaml）**、**代码内置默认值（core/config.py）**。优先级：环境变量 > .env 文件 > 代码默认值。

### 1.1 环境变量（根目录 .env 文件）

> Docker Compose 启动时通过 `env_file: .env` 注入到 `api` 容器；`postgres` 容器通过 `${VAR}` 插值读取。

| 变量名 | 默认值 | 必填 | 说明 |
|--------|--------|------|------|
| **项目基础** | | | |
| `ENV` | `development` | 否 | 运行环境：`development` / `production` |
| `LOG_LEVEL` | `DEBUG` | 否 | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `APP_CONFIG_FILEPATH` | `config.yaml` | 否 | 应用配置文件路径（api 容器内） |
| **数据库（PostgreSQL）** | | | |
| `POSTGRES_USER` | `postgres` | 否 | 数据库用户名（同时供 postgres 容器与 API 使用，生产环境务必修改） |
| `POSTGRES_PASSWORD` | `postgres` | 否 | 数据库密码（同时供 postgres 容器与 API 使用，生产环境务必修改） |
| `POSTGRES_DB` | `manus` | 否 | 数据库名称（同时供 postgres 容器与 API 使用） |
| `SQLALCHEMY_DATABASE_URI` | - | 是 | 数据库连接字符串，格式：`postgresql+asyncpg://USER:PASS@HOST:5432/DB`（容器内 HOST 为 `postgres`） |
| **缓存（Redis）** | | | |
| `REDIS_HOST` | `redis` | 否 | Redis 服务地址（容器内用服务名 `redis`，本地直连用 `localhost`） |
| `REDIS_PORT` | `6379` | 否 | Redis 端口 |
| `REDIS_DB` | `0` | 否 | Redis 数据库编号 |
| `REDIS_PASSWORD` | - | 否 | Redis 密码（留空表示无密码，生产环境建议设置） |
| **对象存储（OSS）** | | | |
| `OSS_BASE_URL` | - | 是 | OSS 文件上传接口地址（腾讯云 COS 兼容接口） |
| `OSS_BUCKET` | `` | 否 | OSS 存储桶名称 |
| **沙箱（Sandbox）** | | | |
| `SANDBOX_ADDRESS` | - | 否 | 固定沙箱地址（IP/主机名），留空则由 API 通过 Docker SDK 动态创建 |
| `SANDBOX_IMAGE` | `sandbox:latest` | 否 | 沙箱 Docker 镜像名（动态创建时使用） |
| `SANDBOX_NAME_PREFIX` | `sandbox` | 否 | 沙箱容器名前缀（实际名：`sandbox-<8位UUID>`） |
| `SANDBOX_TTL_MINUTES` | `60` | 否 | 沙箱空闲超时（分钟），超时自动销毁释放资源 |
| `SANDBOX_NETWORK` | `IDO-network` | 否 | 沙箱所在 Docker 网络（须与 API 同网络） |
| `SANDBOX_CHROME_ARGS` | - | 否 | Chrome 额外启动参数 |
| `SANDBOX_HTTPS_PROXY` | - | 否 | 沙箱 HTTPS 代理地址 |
| `SANDBOX_HTTP_PROXY` | - | 否 | 沙箱 HTTP 代理地址 |
| `SANDBOX_NO_PROXY` | - | 否 | 沙箱不代理的地址 |
| **MCP 多模态** | | | |
| `MULTIMODAL_API_KEY` | - | 是 | 智谱 BigModel API Key（多模态服务使用） |
| `MULTIMODAL_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4/` | 否 | 多模态 API 地址 |
| `MULTIMODAL_VL_MODEL` | `glm-4.6v` | 否 | 视觉理解模型名 |
| `MULTIMODAL_IMAGE_MODEL` | `cogview-4` | 否 | 图像生成模型名 |
| `MULTIMODAL_ASR_MODEL` | `glm-asr-2512` | 否 | 语音识别模型名 |
| **Docker Compose 端口映射（可选）** | | | |
| `API_PORT` | `8000` | 否 | API 服务对外暴露端口（本地 docker-compose.yml） |
| `MCP_MULTIMODAL_PORT` | `9100` | 否 | MCP 多模态对外暴露端口（本地 docker-compose.yml） |
| `NGINX_HTTP_PORT` | `80` | 否 | Nginx HTTP 端口（生产 docker-compose.prod.yml） |
| `NGINX_HTTPS_PORT` | `443` | 否 | Nginx HTTPS 端口（生产 docker-compose.prod.yml） |
| `SEARXNG_SECRET` | `searxng-IDO-secret` | 否 | SearXNG 实例密钥（生产编排文件使用，本地配置在 settings.yml 中） |

### 1.2 应用配置（api/config.yaml）

> 此文件在 Docker 构建时 `COPY . .` 打包进 API 镜像，修改后需重新构建镜像。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| **llm_config** | | LLM 大模型配置 |
| `provider` | `openai` | LLM 提供商（仅 `openai`，兼容 DeepSeek/GLM/Qwen/Kimi 等 OpenAI 兼容协议） |
| `base_url` | - | LLM API 地址（如 `https://open.bigmodel.cn/api/paas/v4`） |
| `api_key` | - | LLM API Key |
| `model_name` | `glm-5.2` | 模型名称 |
| `temperature` | `0.7` | 生成温度（0-1） |
| `max_tokens` | `8192` | 单次最大生成 Token 数 |
| `thinking_mode` | `enabled` | 思考模式：`enabled`（深度推理）/ `disabled`（快速响应） |
| `reasoning_effort` | `high` | 思考强度：`low` / `medium` / `high` / `max` / `xhigh` |
| `context_window` | `64000` | 上下文窗口大小（Token），超过后自动截断历史 |
| `max_retries` | `5` | LLM 调用最大重试次数（批次50，503 容错增强，1-10 可配） |
| **planner_llm_config** | | 规划 Agent 轻量化配置（可选，不配置则复用 llm_config） |
| （同 llm_config 字段） | - | PlannerAgent 专用，降级到 flash+disabled 可节省 70%+ token 成本与时延 |
| **agent_config** | | Agent 行为配置 |
| `max_iterations` | `300` | Agent 最大迭代次数（防止无限循环，与 4 小时超时联动） |
| `max_retries` | `3` | 工具调用最大重试次数 |
| `max_search_results` | `10` | 单次搜索最大结果数 |
| `session_timeout_seconds` | `14400` | 会话级硬超时（秒），4 小时，超时强制注入总结指令 |
| `session_warning_seconds` | `12000` | 软警告（秒），200 分钟（83%），注入收敛提示 |
| `stream_final_answer` | `true` | 流式输出最终答案（F10-1），降低用户感知时延 |
| `tool_filter_enabled` | `true` | 工具按需装配（F10-6），基于步骤描述关键词过滤工具，降低单轮 token 消耗 |
| **tool_cache_config** | | 工具结果缓存（幂等工具结果缓存，白名单机制） |
| **tool_execution_config** | | 工具并行执行（ReActAgent 多工具并行化，黑名单机制） |
| **idempotent_tool_dedup_config** | | 幂等工具调用去重（P10-1，防止长会话中重复发起相同参数的幂等写操作） |
| **mcp_config** | | MCP 外部工具服务 |
| `mcpServers` | - | MCP 服务列表（键值对，每个含 transport/enabled/url/headers） |
| **a2a_config** | | A2A 外部 Agent 服务 |
| `a2a_servers` | `[]` | A2A Agent 服务地址列表 |
| **search_config** | | 搜索配置 |
| `cache_enabled` | `true` | 是否启用搜索结果缓存（Redis） |
| `cache_ttl_seconds` | `3600` | 缓存过期时间（秒） |
| `cache_key_prefix` | `search` | 缓存键前缀 |
| `fetch_timeout` | `15` | 网页抓取超时（秒） |
| `fetch_max_retries` | `2` | 网页抓取最大重试次数 |
| `fetch_max_chars` | `10000` | 单页抓取最大字符数 |
| `fetch_max_concurrency` | `5` | 网页抓取最大并发数 |
| **deep_research_config** | | 深度研究配置 |
| `max_depth` | `2` | 研究最大深度（迭代搜索层数） |
| `results_per_search` | `5` | 每次搜索结果数 |
| `max_insights` | `20` | 最大洞察数量 |
| `time_limit_seconds` | `120` | 研究时间上限（秒） |

### 1.3 JWT 认证配置（代码内置，core/config.py）

> JWT 密钥可通过环境变量 `SECRET_KEY` 覆盖；认证实现位于 [app/core/security.py](file:///e:/workProjs/i_do/api/app/core/security.py)，使用 `python-jose` + `bcrypt`。

| 配置项 | 环境变量名 | 默认值 | 说明 |
|--------|-----------|--------|------|
| JWT 签名密钥 | `SECRET_KEY` | `IDO-secret-key-change-in-production` | **生产环境务必修改** |
| JWT 签名算法 | `ALGORITHM` | `HS256` | 对称加密算法 |
| Access Token 有效期 | `ACCESS_TOKEN_EXPIRE_MINUTES` | `120`（2 小时） | 访问令牌过期时间（分钟） |
| Refresh Token 有效期 | `REFRESH_TOKEN_EXPIRE_HOURS` | `8`（8 小时） | 刷新令牌过期时间（小时） |

**认证流程**：
1. 注册：`POST /api/auth/register`（需 username + phone + password）
2. 登录：`POST /api/auth/login` → 返回 access_token + refresh_token
3. 请求：Header 携带 `Authorization: Bearer <access_token>`
4. 刷新：`POST /api/auth/refresh`（access_token 过期后用 refresh_token 换新）
5. 登出：`POST /api/auth/logout`（令牌加入 Redis 黑名单）

> **注意**：本项目无内置默认账号，首次部署后需通过注册接口创建用户。注册要求：用户名 3-50 字符、手机号 11 位（`1[3-9]\d{9}`）、密码 6-50 字符。

### 1.4 Nginx 配置

**主配置（nginx/nginx.conf）**：

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `worker_processes` | `auto` | 自动匹配 CPU 核数 |
| `worker_connections` | `1024` | 单 worker 最大连接数 |
| `keepalive_timeout` | `65` | Keep-Alive 超时（秒） |
| `client_max_body_size` | `100m` | 全局上传大小限制 |
| `gzip` | `on` | 压缩启用（comp_level 6） |
| WebSocket map | `$http_upgrade → $connection_upgrade` | WebSocket 协议升级映射 |

**站点配置（nginx/conf.d/default.conf）**：

| 配置项 | 说明 |
|--------|------|
| `upstream ui_backend` | 前端服务地址 `ui:3000`，keepalive 32 |
| `upstream api_backend` | 后端服务地址 `api:8000`，keepalive 32 |
| `location /api/` | API 反代，`proxy_buffering off`（SSE 实时推送），`proxy_read_timeout 86400s`（长连接） |
| `location /` | 前端反代，支持 Next.js HMR WebSocket |
| `client_max_body_size` | 文件上传限制 `100m`（location 级别） |
| HTTPS | 按需启用，需提供 `nginx/ssl/fullchain.pem` 与 `privkey.pem`，取消注释 HTTPS server 块 |

### 1.5 服务端口清单

| 服务 | 容器内端口 | 本地部署暴露 | 生产部署暴露 | 说明 |
|------|-----------|-------------|-------------|------|
| nginx | 80 / 443 | - | 80 / 443 | 统一入口（仅生产） |
| api | 8000 | 8000 | 内部 | FastAPI 后端（单 uvicorn worker） |
| ui | 3000 | 本地 pnpm 运行 | 内部 | Next.js 前端（standalone） |
| postgres | 5432 | 内部 | 内部 | 数据库（PostgreSQL 15） |
| redis | 6379 | 内部 | 内部 | 缓存/消息队列（Redis 7.2.4） |
| mcp-multimodal | 9100 | 9100 | 内部 | 多模态工具（8 个工具） |
| searxng | 8080 | 内部 | 内部 | 元搜索引擎 |
| sandbox | 8080 / 9222 / 5900 / 5901 | 动态 | 动态 | 沙箱（8080=FastAPI, 9222=CDP, 5900=VNC, 5901=VNC-WS） |

### 1.6 Redis 客户端配置（代码内置，api/app/infrastructure/storage/redis.py）

| 参数 | 值 | 说明 |
|------|-----|------|
| `socket_timeout` | `30` | 单次读写超时（秒），适配长任务（deep_research/browser） |
| `socket_connect_timeout` | `10` | 建立连接超时（秒） |
| `health_check_interval` | `30` | 健康检查间隔（秒），自动发现并重建失效连接 |
| `max_connections` | `50` | 连接池上限 |
| `decode_responses` | `True` | 自动解码为字符串 |

> Redis `maxmemory`：本地 docker-compose.yml 为 `256mb`，生产 docker-compose.prod.yml 为 `512mb`。

---

## 二、本地部署前置环境要求 + 分步部署实现方案

本地部署采用**前后端分离运行**模式：后端基础设施通过 Docker Compose 容器化运行，前端 UI 通过 Node.js 本地运行（便于热更新开发调试）。

### 2.1 前置环境要求

| 要求 | 最低版本 | 推荐版本 | 说明 |
|------|---------|---------|------|
| 操作系统 | Windows 10 / macOS 12 / Ubuntu 20.04 | Windows 11 / macOS 14 / Ubuntu 22.04 | 需支持 Docker |
| Docker Desktop | 27.0 | 27.5+ | 含 Docker Engine + Compose v2 |
| Docker Compose | v2.20 | v2.32+ | Docker Desktop 内置（使用 `docker compose` 命令） |
| Node.js | 20.x LTS | 22.x LTS | 前端构建运行 |
| pnpm | 9.x | 10.x | 前端包管理（`npm i -g pnpm` 或 `corepack enable`） |
| 系统内存 | 8 GB | 16 GB+ | 沙箱镜像构建需要较多内存 |
| 磁盘空间 | 20 GB | 40 GB+ | 镜像（sandbox 约 3.3GB） + 数据卷 |
| CPU | 4 核 | 8 核+ | 沙箱构建与运行 |

### 2.2 分步部署实现方案

#### 第 1 步：克隆项目并准备配置

```powershell
# 1.进入项目根目录
cd e:\workProjs\i_do

# 2.确认 .env 文件存在（已随项目提供）
# 若不存在，从模板复制：
Copy-Item api\.env.example .env
```

编辑 `.env`，按实际环境填写（关键项）：

```bash
# 数据库（本地部署使用 docker-compose 中的 postgres 容器）
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=manus
SQLALCHEMY_DATABASE_URI=postgresql+asyncpg://postgres:postgres@postgres:5432/manus

# Redis（使用 docker-compose 中的 redis 容器）
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=

# OSS 对象存储（填写实际地址）
OSS_BASE_URL=http://your-oss-endpoint/uploadFile
OSS_BUCKET=

# MCP 多模态 API Key（智谱 BigModel）
MULTIMODAL_API_KEY=your_bigmodel_api_key_here
```

#### 第 2 步：配置 AI 大模型

编辑 `api/config.yaml`，配置 LLM 与外部服务：

```yaml
llm_config:
  provider: openai
  base_url: https://open.bigmodel.cn/api/paas/v4    # 智谱 GLM
  api_key: your_api_key_here
  model_name: glm-5.2
  temperature: 0.7
  max_tokens: 8192
  thinking_mode: enabled
  reasoning_effort: high
  context_window: 64000
  max_retries: 5    # LLM 调用最大重试次数（批次50，503 容错增强）

agent_config:
  max_iterations: 300
  max_retries: 3
  max_search_results: 10
  session_timeout_seconds: 14400    # 会话级硬超时（秒），4 小时

mcp_config:
  mcpServers:
    amap-maps-streamableHTTP:
      transport: streamable_http
      enabled: true
      url: https://mcp.amap.com/mcp?key=your_amap_key
    mcp-multimodal:
      transport: streamable_http
      enabled: true
      url: http://mcp-multimodal:9100/mcp

a2a_config:
  a2a_servers: []
```

> **支持的模型**：任何兼容 OpenAI API 格式的服务均可接入（DeepSeek、通义千问、智谱 GLM、Kimi、本地 Ollama 等），只需修改 `base_url`、`api_key`、`model_name`。
>
> **注意**：`config.yaml` 在 Docker 构建时打包进镜像，修改后需重新构建 API 镜像（`docker compose build api`）。

#### 第 3 步：启动后端基础设施与 API 服务

**方式 A：一键部署脚本（Windows 推荐）**

```powershell
.\deploy.ps1
```

脚本自动完成：环境检查 → .env 检测 → 沙箱镜像构建 → MCP 多模态构建 → API 构建 → 服务启动 → 健康检查。

**方式 B：手动 Docker Compose 部署**

```bash
# 构建并启动后端全部服务（redis/postgres/searxng/mcp-multimodal/sandbox/api）
docker compose up -d --build

# 查看服务状态
docker compose ps

# 等待 API 健康检查通过（约 30-60 秒，首次构建沙箱镜像约 10-15 分钟）
```

> **数据库迁移自动执行**：API 容器启动时 `lifespan` 会自动运行 `alembic upgrade head`，无需手动执行迁移。

#### 第 4 步：验证后端服务

```bash
# 1.查看服务状态（应全部为 healthy）
docker compose ps

# 2.验证 API 状态
curl http://localhost:8000/api/status

# 3.验证 MCP 多模态
curl http://localhost:9100/health

# 4.验证 SearXNG（容器内访问，端口未暴露到宿主机）
docker compose exec searxng wget -qO- http://127.0.0.1:8080/healthz

# 5.访问 API 文档
# 浏览器打开 http://localhost:8000/docs
```

预期输出（`docker compose ps`）：

```
NAME              STATUS                   PORTS
api               Up (healthy)             0.0.0.0:8000->8000/tcp
mcp-multimodal    Up (healthy)             0.0.0.0:9100->9100/tcp
postgres          Up (healthy)             5432/tcp
redis             Up (healthy)             6379/tcp
searxng           Up (healthy)             8080/tcp
```

> **sandbox 容器状态为 Exited 是正常行为**：docker-compose 中的 sandbox 服务仅用于构建 `sandbox:latest` 镜像，实际沙箱容器由 API 通过 Docker SDK 在用户发起会话时按需创建。

#### 第 5 步：启动前端 UI（本地 Node.js 运行）

```powershell
# 进入前端目录
cd ui

# 安装依赖（首次执行）
pnpm install

# 启动开发服务器
pnpm dev
```

#### 第 6 步：配置前端 API 地址

前端默认通过 `NEXT_PUBLIC_API_BASE_URL` 环境变量指定后端地址。本地开发时，在 `ui/` 目录创建 `.env.local`：

```bash
# 后端 API 地址（本地部署指向本机 8000 端口）
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api
```

> **重要**：若不创建 `.env.local`，前端会使用 [ui/src/lib/api/fetch.ts](file:///e:/workProjs/i_do/ui/src/lib/api/fetch.ts) 中的硬编码兜底地址 `http://10.235.127.227:8000/api`（开发环境遗留），需务必通过 `.env.local` 覆盖为实际地址。

#### 第 7 步：注册账号并登录

本项目**无内置默认账号**，首次使用需注册：

```bash
# 方式 A：通过 API 注册
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","phone":"13800138000","password":"admin123"}'

# 方式 B：浏览器打开 http://localhost:3000/register 页面注册
```

注册要求：
- 用户名：3-50 字符
- 手机号：11 位中国手机号（`1[3-9]` 开头）
- 密码：6-50 字符

注册成功后，浏览器打开 `http://localhost:3000` 使用注册的账号登录。

#### 第 8 步：查看日志（按需）

```bash
# 查看所有服务日志
docker compose logs -f

# 查看指定服务日志
docker compose logs -f api
docker compose logs -f mcp-multimodal
```

---

## 三、线上服务端部署硬件 / 软件条件 + 完整部署实施步骤

线上服务端部署采用**全栈 Docker 化**模式：通过 `docker-compose.prod.yml` 编排全部 8 个服务（含 UI + Nginx），仅对外暴露 Nginx 80/443 端口，各服务配置资源限制。

### 3.1 硬件条件

| 资源 | 最低配置 | 推荐配置（支撑 20 并发） | 说明 |
|------|---------|------------------------|------|
| CPU | 4 核 | 8 核+ | 沙箱构建与并发任务调度 |
| 内存 | 8 GB | 32 GB+ | 每个沙箱容器约 1.5GB，API/PG/Redis 各 0.5-1GB |
| 磁盘 | 50 GB SSD | 100 GB+ SSD | 镜像（sandbox 约 3.3GB） + 数据卷 + 沙箱临时文件 |
| 网络带宽 | 10 Mbps | 50 Mbps+ | SSE 流式输出 + 搜索抓取 |
| 公网 IP | 1 个 | 1 个 | 需开放 80/443 端口 |

> **沙箱内存是主要瓶颈**：每个活跃会话的沙箱容器（Ubuntu + Chromium + VNC + Python + LibreOffice）约占 1.5GB 内存。20 并发会话需预留 30GB+ 内存。

### 3.2 软件条件

| 要求 | 最低版本 | 推荐版本 | 说明 |
|------|---------|---------|------|
| 操作系统 | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS | 推荐 Linux 部署 |
| Docker Engine | 27.0 | 27.5+ | 容器运行时 |
| Docker Compose | v2.20 | v2.32+ | 服务编排（v2 语法 `docker compose`） |
| Git | 2.30 | 2.40+ | 代码拉取 |
| SSL 证书 | - | Let's Encrypt / 商业证书 | HTTPS（推荐） |

### 3.3 完整部署实施步骤

#### 第 1 步：服务器初始化

```bash
# 1.更新系统
sudo apt update && sudo apt upgrade -y

# 2.安装 Docker（官方脚本）
curl -fsSL https://get.docker.com | sudo sh

# 3.将当前用户加入 docker 组（免 sudo）
sudo usermod -aG docker $USER
newgrp docker

# 4.验证安装
docker --version
docker compose version
```

#### 第 2 步：拉取项目代码

```bash
# 克隆项目（或通过 scp 上传代码包）
git clone <your-repo-url> /opt/i_do
cd /opt/i_do
```

#### 第 3 步：配置环境变量

```bash
# 创建生产环境配置
cp api/.env.example .env

# 编辑 .env，务必修改以下项
vi .env
```

**生产环境必改项**：

```bash
# 运行环境
ENV=production
LOG_LEVEL=INFO

# 数据库（务必修改密码，postgres 容器与 API 共用此配置）
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<强密码>
POSTGRES_DB=manus
SQLALCHEMY_DATABASE_URI=postgresql+asyncpg://postgres:<强密码>@postgres:5432/manus

# Redis（务必设置密码）
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=<强密码>

# OSS 对象存储
OSS_BASE_URL=http://your-oss-endpoint/uploadFile
OSS_BUCKET=

# MCP 多模态
MULTIMODAL_API_KEY=<your_bigmodel_api_key>

# JWT 密钥（务必修改）
SECRET_KEY=<随机强密码字符串>

# SearXNG 密钥
SEARXNG_SECRET=<随机字符串>
```

> **注意**：Redis 密码设置后，需确认 docker-compose.prod.yml 中 redis 服务的 `command` 添加 `--requirepass <密码>`，或通过 Redis 配置文件设置。当前 docker-compose.prod.yml 未配置 Redis 密码认证命令，如需密码认证需手动添加。

#### 第 4 步：配置 AI 大模型与外部服务

```bash
vi api/config.yaml
```

修改 `llm_config` 中的 `api_key`、`base_url`、`model_name` 为实际值。按需调整 `mcp_config` 中的 MCP 服务地址（生产环境移除 `host.docker.internal` 引用，改为实际服务地址或移除 `system` 配置）。

> **config.yaml 修改后需重新构建 API 镜像**：`docker compose -f docker-compose.prod.yml build api`

#### 第 5 步：构建并启动全部服务

```bash
# 使用生产编排文件构建并启动（首次构建约 15-25 分钟，沙箱镜像较大约 3.3GB）
docker compose -f docker-compose.prod.yml up -d --build
```

#### 第 6 步：验证服务状态

```bash
# 1.查看全部服务状态（应全部为 healthy）
docker compose -f docker-compose.prod.yml ps

# 2.验证 Nginx 入口
curl http://localhost/api/status

# 3.验证前端页面
curl -I http://localhost/

# 4.验证 API 文档
curl http://localhost/docs
```

预期输出：

```
NAME              STATUS                   PORTS
api               Up (healthy)             8000/tcp
mcp-multimodal    Up (healthy)             9100/tcp
nginx             Up                       0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
postgres          Up (healthy)             5432/tcp
redis             Up (healthy)             6379/tcp
searxng           Up (healthy)             8080/tcp
ui                Up (healthy)             3000/tcp
```

#### 第 7 步：注册账号并登录

本项目**无内置默认账号**，首次部署后需注册：

```bash
# 通过 Nginx 入口注册
curl -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","phone":"13800138000","password":"<强密码>"}'
```

或浏览器访问 `http://<服务器IP>/register` 页面注册。

> **生产环境务必使用强密码**（密码 6-50 字符）。

#### 第 8 步（可选）：启用 HTTPS

```bash
# 1.准备 SSL 证书
mkdir -p nginx/ssl
# 将证书文件放入：
#   nginx/ssl/fullchain.pem   证书链
#   nginx/ssl/privkey.pem     私钥

# 2.编辑 nginx/conf.d/default.conf，取消 HTTPS server 块注释
# 3.在 HTTP server 块中取消 301 跳转注释，强制 HTTPS
# 4.重启 Nginx
docker compose -f docker-compose.prod.yml restart nginx
```

#### 第 9 步（可选）：配置防火墙

```bash
# 仅开放必要端口
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable
```

### 3.4 生产环境部署检查清单

- [ ] `.env` 中 `ENV=production`、`LOG_LEVEL=INFO`
- [ ] 数据库密码已修改（`POSTGRES_PASSWORD`）
- [ ] `SQLALCHEMY_DATABASE_URI` 中的密码与 `POSTGRES_PASSWORD` 一致
- [ ] Redis 密码已设置（`REDIS_PASSWORD`），且 redis 服务已配置密码认证
- [ ] `SECRET_KEY`（JWT 密钥）已修改为随机强密码
- [ ] `api/config.yaml` 中 LLM API Key 已配置
- [ ] `MULTIMODAL_API_KEY` 已配置
- [ ] `OSS_BASE_URL` 已配置为实际地址
- [ ] `SEARXNG_SECRET` 已设置为随机字符串
- [ ] 已通过注册接口创建首个管理员账号
- [ ] HTTPS 证书已配置（推荐）
- [ ] 防火墙仅开放 22/80/443 端口
- [ ] 数据卷已配置定期备份（`postgres_data`、`redis_data`）

### 3.5 生产环境资源限制（docker-compose.prod.yml）

| 服务 | 内存限制 | 说明 |
|------|---------|------|
| redis | 768 MB | 含 maxmemory 512mb + 连接开销 |
| postgres | 1 GB | 连接池 + 查询缓存 |
| mcp-multimodal | 1 GB | Playwright Chromium |
| searxng | 512 MB | 元搜索 |
| api | 2 GB | FastAPI + Docker SDK + Playwright |
| ui | 512 MB | Next.js standalone |
| nginx | 256 MB | 反向代理 |

---

## 附加：100 人并发承载力评估与优化方向

### 评估结论

**当前架构无法在单机上稳定支撑 100 人同时在线会话**，核心瓶颈是沙箱容器内存开销。纯对话场景（不触发沙箱）可支撑；涉及沙箱操作（代码执行、浏览器自动化）的场景，单机上限约 20-30 并发。

### 量化分析

#### 各组件资源占用（单实例）

| 组件 | 内存占用 | CPU 占用 | 说明 |
|------|---------|---------|------|
| nginx | ~50 MB | <1% | 反向代理，轻量 |
| ui | ~200 MB | 1-3% | Next.js standalone |
| api | ~500 MB-1 GB | 5-15% | 单 uvicorn worker（`run.sh` 无 `--workers` 参数），异步 |
| postgres | ~256 MB-1 GB | 2-5% | 连接池 + 查询 |
| redis | ~512 MB | 1-3% | SSE 事件流 + 缓存 + Token 黑名单（maxmemory 512mb） |
| mcp-multimodal | ~500 MB-1 GB | 2-5% | Playwright Chromium |
| searxng | ~100-200 MB | 1-2% | 元搜索 |
| **sandbox（每个）** | **~1.5-2 GB** | **10-30%** | Ubuntu + Chromium + VNC + Python 3.10 + LibreOffice |

#### 100 并发会话资源需求

| 场景 | 内存需求 | 瓶颈分析 |
|------|---------|---------|
| **纯对话**（无沙箱） | ~3 GB（服务） + LLM API 并发 | LLM API 速率限制 |
| **轻度沙箱**（30% 会话触发） | ~3 GB + 30×1.5GB = **48 GB** | 内存 + LLM API |
| **重度沙箱**（100% 会话触发） | ~3 GB + 100×1.5GB = **153 GB** | **内存（不可行）** |

#### 瓶颈识别

| 瓶颈 | 严重程度 | 说明 |
|------|---------|------|
| **沙箱容器内存** | 致命 | 每个沙箱 1.5GB（Ubuntu+Chromium+VNC+Python+LibreOffice），100 并发需 150GB，单机不可行 |
| **LLM API 速率限制** | 高 | 100 并发请求可能触发提供商限流（GLM/DeepSeek 等） |
| **API 单进程** | 中 | `run.sh` 启动单 uvicorn worker，JSON 解析与 SSE 序列化单核瓶颈 |
| **Redis 连接池** | 中 | `max_connections=50`，100 并发 SSE 连接可能耗尽 |
| **PostgreSQL 连接池** | 中 | 默认连接池上限，100 并发会话可能排队 |
| **Docker Socket** | 中 | 沙箱动态创建/销毁走 Docker API，高并发下可能成为瓶颈 |

### 优化方向

#### 方向一：沙箱资源优化（最高优先级）

1. **沙箱复用**：多个轻量会话共享沙箱（通过命名空间隔离），减少容器数量
2. **沙箱池化**：预创建 N 个空闲沙箱，按需分配，避免冷启动延迟
3. **轻量沙箱**：将沙箱基础镜像从 Ubuntu 切换为 Alpine + headless Chromium，内存降至 ~500MB
4. **按需创建**：仅当任务确实需要代码执行/浏览器时才创建沙箱，纯对话不创建
5. **沙箱远程化**：将沙箱调度到独立的 Docker Swarm/K8s 集群，API 通过远程 Docker Socket 管理

#### 方向二：API 水平扩展

1. **多 worker**：`run.sh` 修改为 `uvicorn --workers 4` 启动多进程，充分利用多核 CPU
2. **多实例**：`docker compose up --scale api=4`，Nginx 上游负载均衡
3. **SSE 独立**：将 SSE 推送与业务 API 分离，SSE 走专用网关

#### 方向三：基础设施调优

1. **Redis 集群**：`max_connections` 提升至 200，或部署 Redis Cluster 分担 SSE 连接
2. **PostgreSQL 连接池**：引入 PgBouncer，连接复用降低开销
3. **Redis Stream 消费组**：SSE 事件改用消费组模式，支持多 worker 分摊推送

#### 方向四：LLM API 层优化

1. **请求队列**：引入令牌桶限流，控制 LLM API 并发在提供商限额内
2. **多 Key 轮询**：配置多个 API Key，请求轮询分发，提升总并发
3. **响应缓存**：对相同查询的 LLM 响应做缓存，减少重复请求
4. **流式降级**：LLM API 限流时，自动降级为非流式 + 前端轮询

#### 方向五：架构演进

1. **Kubernetes 编排**：沙箱改为 K8s Pod，按需调度到集群节点，突破单机内存限制
2. **沙箱即服务**：沙箱抽象为独立微服务，支持远程调度与弹性伸缩
3. **会话优先级**：高并发时按用户优先级排队，保证核心用户体验

### 推荐演进路线

| 阶段 | 目标并发 | 关键动作 | 预估内存 |
|------|---------|---------|---------|
| 当前 | 10-20 | 单机 docker-compose | 32 GB |
| 优化一 | 30-50 | 沙箱池化 + API 多 worker + Redis 调优 | 64 GB |
| 优化二 | 50-80 | 沙箱远程化 + 多 Key 轮询 + PgBouncer | 96 GB + 远程沙箱节点 |
| 优化三 | 100+ | K8s 编排 + 沙箱即服务 + LLM 请求队列 | 集群化 |

---

## 常用运维命令

```bash
# ========== 本地部署（docker-compose.yml）==========
docker compose up -d --build          # 构建并启动后端
docker compose ps                      # 查看服务状态
docker compose logs -f api             # 查看 API 日志
docker compose down                    # 停止全部服务
docker compose down -v                 # 停止并清除数据卷（删数据，慎用）

# ========== 生产部署（docker-compose.prod.yml）==========
docker compose -f docker-compose.prod.yml up -d --build   # 构建并启动全栈
docker compose -f docker-compose.prod.yml ps               # 查看服务状态
docker compose -f docker-compose.prod.yml logs -f nginx    # 查看 Nginx 日志
docker compose -f docker-compose.prod.yml restart api      # 重启 API
docker compose -f docker-compose.prod.yml down             # 停止全部服务

# ========== 数据库 ==========
# 迁移在 API 启动时自动执行（alembic upgrade head），无需手动运行
# 如需手动执行：
docker compose exec api alembic upgrade head                          # 执行迁移
docker compose exec api alembic revision --autogenerate -m "描述"      # 生成迁移脚本
docker compose exec postgres pg_dump -U postgres manus > backup.sql   # 备份数据库
docker compose exec -T postgres psql -U postgres manus < backup.sql   # 恢复数据库

# ========== 沙箱管理 ==========
docker ps --filter "name=sandbox"                  # 查看运行中的沙箱
docker rm -f sandbox-<8位UUID>                     # 手动销毁沙箱
docker images | grep sandbox                       # 查看沙箱镜像

# ========== 前端 UI（本地开发）==========
cd ui
pnpm install                                       # 安装依赖
pnpm dev                                           # 启动开发服务器（端口 3000）
pnpm build                                         # 构建生产产物
pnpm test                                          # 运行单元测试（vitest）
```

---

## 常见问题

### Q1: 启动后 API 服务一直重启？

1. 检查 PostgreSQL 和 Redis 是否健康：`docker compose ps`
2. 检查 `.env` 中 `SQLALCHEMY_DATABASE_URI` 是否正确（密码、主机名 `postgres`、库名 `manus`）
3. 检查 `api/config.yaml` 中 LLM API Key 是否有效
4. 查看日志：`docker compose logs api`

### Q2: 沙箱容器创建失败？

1. 确保 Docker Socket 已挂载（compose 中 `api` 服务配置了 `/var/run/docker.sock`）
2. 确保 `sandbox:latest` 镜像已构建：`docker images | grep sandbox`
3. 确保沙箱网络 `IDO-network` 存在：`docker network ls | grep IDO`

### Q3: 搜索功能不工作？

1. 检查 SearXNG 容器是否健康：`docker compose ps searxng`
2. 国内服务器可能需配置代理：`.env` 中设置 `SANDBOX_HTTPS_PROXY`
3. 系统会自动降级到 Bing 搜索作为备用

### Q4: 前端无法连接后端？

1. 本地开发：确认 `ui/.env.local` 中 `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api`
2. **若不创建 `.env.local`，前端会使用硬编码地址 `http://10.235.127.227:8000/api`（开发环境遗留），需务必覆盖**
3. 生产部署：确认 Nginx 配置正确，`/api/` 反代到 `api:8000`
4. 检查浏览器控制台 CORS 错误（后端已配置 `allow_origins=["*"]`）

### Q5: 如何更换 AI 模型？

编辑 `api/config.yaml` 的 `llm_config`，修改 `base_url`、`api_key`、`model_name`。任何兼容 OpenAI API 格式的服务均可接入。修改后需重新构建 API 镜像：`docker compose build api`。

### Q6: 如何添加新的 MCP 工具？

在 `api/config.yaml` 的 `mcp_config.mcpServers` 下添加：

```yaml
mcp_config:
  mcpServers:
    your-mcp-service:
      transport: streamable_http    # 或 stdio / sse
      enabled: true
      url: http://your-service:port/mcp
```

### Q7: 如何创建用户账号？

本项目无内置默认账号，需通过注册接口创建：

```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","phone":"13800138000","password":"admin123"}'
```

或浏览器访问 `http://localhost:3000/register` 页面注册。注册要求：用户名 3-50 字符、手机号 11 位（`1[3-9]` 开头）、密码 6-50 字符。

### Q8: 修改 config.yaml 后如何生效？

`config.yaml` 在 Docker 构建时打包进 API 镜像，修改后需重新构建：

```bash
# 本地
docker compose build api && docker compose up -d api

# 生产
docker compose -f docker-compose.prod.yml build api && docker compose -f docker-compose.prod.yml up -d api
```

---

## 技术栈总览

| 层级 | 技术 | 说明 |
|------|------|------|
| **前端** | Next.js 16.1 + React 19.2 | 服务端渲染 + 客户端交互，output: standalone |
| | Tailwind CSS 4 + Radix UI | 样式与组件库 |
| | noVNC | 浏览器内远程桌面（沙箱 VNC） |
| | pnpm | 包管理（corepack 激活） |
| **后端** | Python 3.12 + FastAPI | 异步高性能 API 框架 |
| | SQLAlchemy 2.0 + Alembic + asyncpg | 异步 ORM + 数据库迁移 + PostgreSQL 驱动 |
| | Pydantic Settings | 类型安全的环境变量管理 |
| | Docker SDK (python-docker) | 沙箱容器动态管理 |
| | Playwright | 浏览器自动化（CDP 协议） |
| | python-jose + bcrypt | JWT 认证 + 密码哈希 |
| | uvicorn | ASGI 服务器（单 worker） |
| **AI** | OpenAI 兼容 API | LLM 接入（DeepSeek/GLM/Qwen/Kimi 等） |
| | MCP 协议 (streamable-http) | 外部工具集成 |
| | A2A 协议 | Agent 互联 |
| **基础设施** | PostgreSQL 15 | 关系型数据库 |
| | Redis 7.2.4 | 缓存 + 消息队列 + SSE 事件流 + Token 黑名单 |
| | Nginx 1.27 | 反向代理 + 负载均衡 + SSE/WebSocket |
| | Docker Compose v2 | 服务编排 |
| **沙箱** | Ubuntu 22.04 | 隔离执行环境 |
| | Chromium + CDP (9222) | 浏览器自动化 |
| | Xvfb + x11vnc + websockify (5900/5901) | 虚拟显示 + VNC 远程桌面 |
| | Supervisor | 多进程管理 |
| | Python 3.10 + Node.js 24 | 代码执行环境 |
| | LibreOffice + poppler + qpdf + pandoc | 文档处理工具链 |
| **搜索** | SearXNG | 元搜索引擎（Google/Bing/DDG/Wikipedia/Brave 聚合） |
| **多模态** | 智谱 BigModel | glm-4.6v (视觉) / cogview-4 (绘图) / glm-asr-2512 (语音) |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [环境部署.md](docs/环境部署.md) | 部署快速参考（参考若依框架规范），涵盖准备工作、运行系统、必要配置、部署系统、环境变量、Nginx 配置、常见问题 |
| [架构演进与优化方案](docs/optimization/architect_optimization_plan.md) | 系统架构演进记录、批次优化方案与 E2E 验证报告 |
| [提示词冲突优化与异步退避修复方案](.trae/documents/提示词冲突优化与异步退避修复方案.md) | 提示词架构修复方案（文档生成能力边界澄清、异步任务 B1/B2 矛盾消除） |

---

## 支持项目

如果这个项目对你有帮助，感觉不错请喝杯咖啡吧：

<p align="center">
  <img src="example-image/感觉不错请喝杯咖啡吧.jpg" alt="感觉不错请喝杯咖啡吧" width="300" />
</p>

---

## 加入群聊

欢迎加入交流群，一起探讨 AI Agent 的使用与开发：

<p align="center">
  <img src="example-image/加入群聊.jpg" alt="加入群聊" width="300" />
</p>
