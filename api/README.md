# I-DO API 服务 · 工程文档

> 基于 FastAPI 构建的后端中控服务，提供会话管理、AI Agent 调度（Planner + ReAct）、文件处理、沙箱隔离执行、MCP/A2A 工具集成、SSE 流式输出、VNC 远程桌面代理等核心能力。
>
> 本文档面向二次开发与运维人员，覆盖：**架构说明 → 配置详解 → 环境搭建 → 本地部署 → 问题调试 → 二次开发**，按章阅读即可独立完成开发与交付。

---

## 目录

- [一、服务概览](#一服务概览)
- [二、技术栈](#二技术栈)
- [三、项目结构](#三项目结构)
- [四、分层架构与核心数据流](#四分层架构与核心数据流)
- [五、配置说明](#五配置说明)
- [六、API 路由总览](#六api-路由总览)
- [七、本地开发环境搭建](#七本地开发环境搭建)
- [八、本地部署（Docker）](#八本地部署docker)
- [九、数据库迁移](#九数据库迁移)
- [十、测试](#十测试)
- [十一、问题调试手册](#十一问题调试手册)
- [十二、二次开发指引](#十二二次开发指引)
- [十三、常用命令速查](#十三常用命令速查)

---

## 一、服务概览

I-DO API 是整个系统的**核心中控**：向上承接前端 UI 的 HTTP / SSE / WebSocket 请求，向下调度 LLM、沙箱、MCP、A2A、搜索引擎等外部资源，以 ReAct（推理-行动）循环完成复杂任务的自主执行。

### 核心能力

| 能力域 | 说明 |
|--------|------|
| **会话管理** | 任务会话全生命周期：创建、列表 SSE、详情、删除、停止、未读清除 |
| **AI Agent 调度** | PlannerAgent 规划 + ReActAgent 执行的双 Agent 架构，支持工具并行、按需装配、幂等去重 |
| **流式输出** | SSE 实时推送思考过程、工具调用、最终答案切片，支持 Last-Event-ID 断连恢复 |
| **沙箱隔离** | 通过 Docker SDK 动态创建/销毁沙箱容器，执行代码、Shell、浏览器自动化 |
| **文件处理** | 文件上传/下载/读取，OSS 对象存储集成，沙箱内文件操作 |
| **VNC 远程桌面** | WebSocket 代理沙箱 VNC，前端 noVNC 实时观察沙箱操作 |
| **MCP 工具集成** | streamable-http 协议接入外部 MCP 服务（地图、多模态、供应链等） |
| **A2A Agent 互联** | A2A 协议调用外部 Agent |
| **多模态** | 视觉理解、OCR、语音识别、图像生成、视频分析（经 MCP 多模态服务） |
| **认证鉴权** | JWT（access + refresh），Token 黑名单登出，bcrypt 密码哈希 |
| **可观测性** | 指标采集（metrics_collector）、Shell 调用剖析、结构化日志、健康检查 |

### 项目会话能力展示

以下为系统实际运行的能力截图：

**整体执行流程：**

![I-DO 执行流程图](../example-image/流程图.png)

**多模态能力（图像理解 / OCR / 语音 / 图像生成 / 视频分析）：**

![多模态能力](../example-image/多模态.png)

**PPT 生成能力（文档撰写 / PPT 自动生成 / 文件处理）：**

![能力介绍-PPT生成](../example-image/能力介绍-PPT生成.png)

**系统集成与数据分析能力（MCP / A2A / 代码执行 / 数据处理）：**

![系统集成及数据分析能力](../example-image/系统集成及数据分析能力.png)

📎 **完整能力介绍文档**（点击下载）：[I-DO智能办公伙伴介绍.pptx](../example-image/I-DO智能办公伙伴介绍.pptx)

> 系统完整架构图、数据流、并发承载力评估见根目录 [README.md](../README.md)。

---

## 二、技术栈

| 类别 | 技术 | 版本 | 说明 |
|------|------|------|------|
| 运行时 | Python | 3.12+ | 要求 >=3.12（pyproject.toml 约束） |
| Web 框架 | FastAPI | 0.116.1 | 异步 ASGI |
| ASGI 服务器 | uvicorn[standard] | 0.35.0 | 开发 `--reload`，生产单 worker |
| ORM | SQLAlchemy | 2.0.43 | 异步引擎 |
| 数据库驱动 | asyncpg | 0.30.0 | PostgreSQL 异步驱动 |
|      | psycopg2-binary | 2.9.10 | Alembic 迁移用同步驱动 |
| 数据库迁移 | Alembic | 1.16.5 | 启动时自动 `upgrade head` |
| 缓存/消息队列 | Redis | 6.4.0 | 异步客户端，SSE 事件流 + 缓存 + Token 黑名单 |
| 配置管理 | pydantic-settings | 2.10.1 | `.env` 加载，类型安全 |
| LLM 接入 | openai | 1.107.2 | OpenAI 兼容协议（DeepSeek / GLM / Qwen / Kimi） |
| 工具协议 | mcp | 1.22.0 | streamable-http，外部工具集成 |
| 浏览器自动化 | playwright | 1.56.0 | CDP 协议，沙箱内 Chromium |
| 容器管理 | docker | 7.1.0 | Python SDK，沙箱动态创建/销毁 |
| 认证 | python-jose + bcrypt | - | JWT + 密码哈希 |
| HTTP 客户端 | httpx | 0.28.1 | 异步，LLM/MCP/A2A 调用 |
| 网页解析 | beautifulsoup4 + markdownify | - | 搜索结果抓取与清洗 |
| JSON 修复 | json-repair | 0.53.0 | LLM 输出 JSON 容错解析 |
| SSE | sse-starlette | 3.0.3 | Server-Sent Events |
| WebSocket | websockets | 15.0.1 | VNC 代理转发 |
| 包管理 | uv | latest | 依赖安装（比 pip 快 10-100 倍） |
| 测试 | pytest | 8.4.2 | 单元测试 |

> 完整依赖见 [pyproject.toml](pyproject.toml)，锁文件见 [uv.lock](uv.lock)，导出版见 [requirements.txt](requirements.txt)（`uv export` 生成，含哈希，适合离线/生产安装）。

---

## 三、项目结构

```
api/
├── app/                            # 应用主包
│   ├── main.py                     # FastAPI 入口：lifespan / 中间件 / 路由注册 / 默认 admin 播种
│   ├── core/                       # 应用内核
│   │   └── security.py             #   JWT 签发/校验、密码哈希、依赖注入(get_current_user_id)
│   │
│   ├── application/                # 【应用层】业务服务编排，衔接接口层与领域层
│   │   ├── services/
│   │   │   ├── agent_service.py    #   Agent 服务：会话执行、沙箱 TTL、会话锁、事件回放
│   │   │   ├── session_service.py  #   会话服务：CRUD、列表、未读数
│   │   │   ├── user_service.py     #   用户服务：注册/登录/登出/刷新/改密
│   │   │   ├── file_service.py     #   文件服务：上传/下载
│   │   │   ├── file_presentation_service.py  # 文件展示
│   │   │   ├── app_config_service.py #   应用配置（运行时读写 config.yaml）
│   │   │   └── status_service.py   #   健康检查（postgres/redis/cos）
│   │   └── errors/
│   │       └── exceptions.py       #   业务异常（NotFoundError 等）
│   │
│   ├── domain/                     # 【领域层】核心业务逻辑，不依赖外部实现
│   │   ├── models/                 #   领域模型（纯数据结构）
│   │   │   ├── session.py event.py message.py plan.py research.py
│   │   │   ├── user.py file.py skill.py tool_result.py memory.py
│   │   │   ├── search.py health_status.py app_config.py memory_config.py
│   │   ├── repositories/           #   仓储接口（抽象）
│   │   │   ├── uow.py              #     工作单元（Unit of Work）
│   │   │   ├── session_repository.py user_repository.py
│   │   │   ├── file_repository.py skill_repository.py app_config_repository.py
│   │   ├── external/               #   外部服务接口抽象（llm/sandbox/browser/search...）
│   │   └── services/               #   领域服务
│   │       ├── agent_task_runner.py    #   Agent 任务执行器（核心调度循环）
│   │       ├── skill_service.py        #   技能服务
│   │       ├── skills_prompt_cache.py  #   Skills 提示词缓存（L1 内存 + L2 Redis）
│   │       ├── agents/                 #   Agent 实现
│   │       │   ├── base.py             #     Agent 基类
│   │       │   ├── planner.py          #     PlannerAgent（规划）
│   │       │   ├── react.py            #     ReActAgent（执行）
│   │       │   └── task_type_classifier.py  # 任务类型分类
│   │       ├── flows/                  #   执行流程编排
│   │       │   ├── base.py             #     Flow 基类
│   │       │   └── planner_react.py    #     Planner→ReAct 编排
│   │       ├── tools/                  #   工具实现（Agent 可调用的工具）
│   │       │   ├── base.py             #     工具基类
│   │       │   ├── shell.py file.py browser.py search.py
│   │       │   ├── deep_research.py skill.py message.py
│   │       │   ├── mcp.py a2a.py       #     MCP / A2A 工具
│   │       │   ├── tool_search.py      #     工具搜索
│   │       │   ├── concurrency.py      #     工具并行执行
│   │       │   └── budget_tracker.py   #     预算跟踪
│   │       ├── prompts/                #   提示词模板（system/react/planner）
│   │       ├── observability/          #   可观测性（指标采集/持久化/Shell 剖析）
│   │       └── experiments/            #   实验性功能（特性开关解析）
│   │
│   ├── infrastructure/             # 【基础设施层】外部服务具体实现
│   │   ├── storage/                #   存储客户端
│   │   │   ├── postgres.py         #     PostgreSQL 异步引擎 + UoW 实现
│   │   │   ├── redis.py            #     Redis 异步客户端（连接池/健康检查）
│   │   │   ├── cos.py              #     OSS 对象存储
│   │   │   ├── tool_cache.py       #     工具结果缓存
│   │   │   ├── search_cache.py     #     搜索结果缓存
│   │   │   ├── session_prompt_cache.py  # 会话级提示词缓存
│   │   │   ├── idempotent_tool_registry.py  # 幂等工具去重
│   │   │   └── vnc_status_tracker.py   #  VNC 状态跟踪
│   │   ├── repositories/           #   仓储实现（DB / 文件）
│   │   │   ├── db_uow.py           #     Unit of Work 实现
│   │   │   ├── db_session_repository.py db_user_repository.py db_file_repository.py
│   │   │   └── file_skill_repository.py file_app_config_repository.py
│   │   ├── models/                 #   ORM 模型（SQLAlchemy）
│   │   │   ├── base.py session.py user.py file.py
│   │   ├── external/               #   外部服务实现
│   │   │   ├── llm/                #     LLM（OpenAI 兼容，工厂模式 + 流式 + token 计数）
│   │   │   ├── sandbox/            #     Docker 沙箱管理
│   │   │   ├── browser/            #     Playwright 浏览器（视觉点击/无障碍快照/对话框监控）
│   │   │   ├── search/             #     搜索（SearXNG 主 + Bing 兜底 + 内容抓取 + 查询改写）
│   │   │   ├── task/               #     异步任务（Redis Stream）
│   │   │   ├── task_callback/      #     任务回调（Redis Stream）
│   │   │   ├── message_queue/      #     消息队列（Redis Stream）
│   │   │   ├── file_storage/       #     文件存储
│   │   │   ├── health_checker/     #     健康检查（postgres/redis）
│   │   │   └── json_parser/        #     JSON 修复解析
│   │   ├── logging/                #   日志配置
│   │   ├── metrics/                #   指标
│   │   └── db_sanitize.py          #   数据库输入清洗
│   │
│   └── interfaces/                 # 【接口层】HTTP/SSE/WS 端点
│       ├── endpoints/              #   路由定义
│       │   ├── routes.py           #     路由聚合器
│       │   ├── auth_routes.py      #     认证（注册/登录/登出/刷新/改密/me）
│       │   ├── status_routes.py    #     健康检查
│       │   ├── app_config_routes.py#     应用配置
│       │   ├── file_routes.py      #     文件上传/下载
│       │   ├── session_routes.py   #     会话 CRUD + 列表 SSE
│       │   ├── chat_routes.py      #     聊天 SSE 流
│       │   ├── session_file_routes.py   # 会话文件读取
│       │   ├── session_shell_routes.py  # Shell 输出读取
│       │   ├── session_vnc_routes.py    # VNC WebSocket 代理
│       │   └── sandbox_callback_routes.py # 沙箱任务回调（内部）
│       ├── schemas/                #   请求/响应模型（Pydantic）
│       ├── middleware/             #   中间件
│       ├── errors/
│       │   └── exception_handlers.py  # 全局异常处理
│       └── service_dependencies.py    # 依赖注入（服务工厂）
│
├── core/
│   └── config.py                   # 配置管理（Pydantic Settings，加载 .env）
├── config.yaml                     # 应用配置（LLM/MCP/A2A/搜索/Agent/缓存）
├── .env.example                    # 环境变量模板
├── alembic.ini                     # Alembic 配置
├── alembic/                        # 数据库迁移脚本
│   ├── env.py
│   └── versions/                   #   6 个迁移版本
├── pyproject.toml                  # 依赖定义（uv 管理）
├── uv.lock                         # 依赖锁文件
├── requirements.txt                # 导出依赖（含哈希，生产/离线用）
├── Dockerfile                      # 容器镜像构建
├── run.sh                          # 容器启动脚本（生产，无 reload）
├── dev.sh                          # 本地开发启动脚本（--reload）
├── pytest.ini                      # 测试配置
└── README.md                       # 本文档
```

---

## 四、分层架构与核心数据流

### 4.1 分层职责

本项目采用 **DDD 分层架构**，依赖方向严格自上而下：

```
┌─────────────────────────────────────────────────┐
│  interfaces（接口层）                            │  HTTP/SSE/WS 端点、Schema、异常处理
│  ↓ 依赖                                          │
├─────────────────────────────────────────────────┤
│  application（应用层）                           │  业务编排：AgentService / SessionService ...
│  ↓ 依赖                                          │
├─────────────────────────────────────────────────┤
│  domain（领域层）                                │  核心逻辑：Agent / Flow / Tools / Prompts
│  ↓ 依赖（接口抽象，非具体实现）                   │
├─────────────────────────────────────────────────┤
│  infrastructure（基础设施层）                    │  具体实现：Postgres / Redis / LLM / Sandbox
└─────────────────────────────────────────────────┘
```

- **interfaces**：只做协议适配（HTTP→Service 调用），不含业务逻辑。
- **application**：编排领域服务完成用例，管理事务边界（UoW）。
- **domain**：纯业务逻辑，通过 `domain/external/` 与 `domain/repositories/` 定义接口抽象，不依赖具体基础设施。
- **infrastructure**：实现 domain 层定义的接口，对接真实外部服务。

### 4.2 核心数据流（一次聊天请求）

```
前端 POST /api/sessions/{id}/chat
  │  Authorization: Bearer <access_token>
  ▼
interfaces/chat_routes.py
  │  校验会话归属 → 依赖注入 AgentService
  ▼
application/agent_service.py
  │  获取/创建沙箱 → 启动 Agent 任务
  ▼
domain/services/agent_task_runner.py
  │  调用 Flow（Planner→ReAct）
  ▼
domain/services/flows/planner_react.py
  │  1) PlannerAgent 生成 JSON 执行计划
  │  2) ReActAgent 按计划逐步执行
  │     ↓ 每轮循环
  │     ├─ 装配工具（按步骤关键词过滤，降低 token）
  │     ├─ 调用 LLM（OpenAI 兼容）→ 输出 thought + tool_calls
  │     ├─ 并行执行工具（shell/browser/search/mcp/a2a...）
  │     │   └─ 工具经 infrastructure/external/* 调用外部服务
  │     │      └─ 沙箱操作 → Docker SDK 创建/复用 sandbox 容器
  │     ├─ 工具结果缓存 / 幂等去重
  │     └─ 记忆压缩（超阈值时主动/被动压缩上下文）
  │
  │  全程 SSE 推送事件（thought/tool_call/tool_result/final_answer）
  ▼
前端 EventSource 实时接收
  │  支持 Last-Event-ID 断连恢复
```

### 4.3 应用生命周期（[app/main.py](app/main.py)）

`lifespan` 上下文管理器按序执行：

1. **重新初始化日志**（uvicorn 启动会覆盖根 logger 配置）
2. **数据库迁移**：`alembic upgrade head`（生产同步表结构），完成后再次初始化日志（alembic 的 `fileConfig` 会降级日志级别）
3. **初始化存储客户端**：Redis / PostgreSQL / COS
4. **播种默认管理员**：幂等创建 `admin/admin123`（已存在则跳过，失败不阻塞启动）
5. **初始化 Skills 提示词缓存**：L1 内存 + L2 Redis
6. `yield` 进入服务期
7. 关闭时：等待 AgentService 优雅关闭（30s 超时）→ 关闭 Redis/PG/COS

> **默认账号**：系统启动时自动播种 `admin / admin123`（[main.py:43-66](app/main.py)），可直接登录使用。

---

## 五、配置说明

配置分三层，优先级：**环境变量（.env）> 应用配置（config.yaml）> 代码默认值（core/config.py）**。

### 5.1 环境变量（`.env`）

由 [core/config.py](core/config.py) 的 `Settings` 类通过 `pydantic-settings` 加载，模板见 [.env.example](.env.example)。

#### 5.1.1 项目基础

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENV` | `development` | 运行环境：`development` / `production` |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `APP_CONFIG_FILEPATH` | `config.yaml` | 应用配置文件路径 |

#### 5.1.2 数据库（PostgreSQL）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SQLALCHEMY_DATABASE_URI` | `postgresql+asyncpg://postgres:postgres@localhost:5432/manus` | 异步连接串。**Docker 部署时主机改为 `postgres`** |
| `POSTGRES_USER` | `postgres` | postgres 容器用（compose 插值） |
| `POSTGRES_PASSWORD` | `postgres` | postgres 容器用，**生产务必修改** |
| `POSTGRES_DB` | `manus` | postgres 容器用 |

> **本地开发**：若本机已装 PostgreSQL，URI 保持 `localhost:5432/manus`；若用 docker-compose 的 postgres，改为 `postgres:5432/manus`。

#### 5.1.3 Redis

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `REDIS_HOST` | `localhost` | Redis 地址（Docker 部署改为 `redis`） |
| `REDIS_PORT` | `6379` | Redis 端口 |
| `REDIS_DB` | `0` | Redis 库编号 |
| `REDIS_PASSWORD` | `None` | Redis 密码（生产建议设置） |

> 客户端参数（[infrastructure/storage/redis.py](app/infrastructure/storage/redis.py)）：`socket_timeout=30`、`socket_connect_timeout=10`、`health_check_interval=30`、`max_connections=50`。

#### 5.1.4 对象存储（OSS）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `OSS_BASE_URL` | `""` | OSS 文件上传接口地址（腾讯云 COS 兼容） |
| `OSS_BUCKET` | `` | 存储桶名 |

#### 5.1.5 沙箱（Sandbox）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SANDBOX_ADDRESS` | `None` | 固定沙箱地址，留空则由 Docker SDK 动态创建 |
| `SANDBOX_IMAGE` | `None` | 沙箱镜像名（动态创建用，默认 `sandbox:latest`） |
| `SANDBOX_NAME_PREFIX` | `None` | 容器名前缀（实际 `sandbox-<8位UUID>`） |
| `SANDBOX_TTL_MINUTES` | `60` | 容器内 supervisord 服务超时（分钟） |
| `SANDBOX_IDLE_TTL_SECONDS` | `7200` | 中控侧空闲销毁 TTL（秒），会话结束 2 小时后自动销毁，TTL 内续接可复用 |
| `SANDBOX_NETWORK` | `None` | 沙箱 Docker 网络（须与 API 同网络，默认 `IDO-network`） |
| `SANDBOX_CHROME_ARGS` | `""` | Chrome 额外启动参数 |
| `SANDBOX_HTTPS_PROXY` / `SANDBOX_HTTP_PROXY` / `SANDBOX_NO_PROXY` | `None` | 沙箱代理配置 |

#### 5.1.6 JWT 认证（[app/core/security.py](app/core/security.py)）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SECRET_KEY` | `IDO-secret-key-change-in-production` | JWT 签名密钥，**生产务必修改** |
| `ALGORITHM` | `HS256` | 签名算法 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `120` | Access Token 有效期（分钟，2 小时） |
| `REFRESH_TOKEN_EXPIRE_HOURS` | `8` | Refresh Token 有效期（小时） |

#### 5.1.7 MCP 多模态

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `MULTIMODAL_API_KEY` | `""` | 智谱 BigModel API Key |
| `MULTIMODAL_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4/` | 多模态 API 地址 |
| `MULTIMODAL_VL_MODEL` | `glm-4.6v` | 视觉理解模型 |
| `MULTIMODAL_IMAGE_MODEL` | `cogview-4` | 图像生成模型 |
| `MULTIMODAL_ASR_MODEL` | `glm-asr-2512` | 语音识别模型 |

### 5.2 应用配置（[config.yaml](config.yaml)）

> 此文件在 Docker 构建时 `COPY . .` 打包进镜像，**修改后需重新构建 API 镜像**：`docker compose build api`。

#### 5.2.1 LLM 配置（`llm_config`）

主 Agent 使用的 LLM，兼容 OpenAI 协议：

```yaml
llm_config:
  provider: openai
  base_url: https://api.deepseek.com        # DeepSeek / GLM / Qwen / Kimi / 本地 Ollama
  api_key: sk-yourkey
  model_name: deepseek-v4-flash
  temperature: 0.7
  max_tokens: 8192
  thinking_mode: enabled                    # enabled 深度推理 / disabled 快速响应
  reasoning_effort: medium                  # low / medium / high / max / xhigh
  context_window: 128000                    # 上下文窗口（Token），超阈值触发记忆压缩
  max_retries: 5                            # 503/502 自动指数退避重试
  supports_image_input: false               # 是否多模态（影响工具截图发送方式）
```

#### 5.2.2 规划 Agent 配置（`planner_llm_config`，可选）

PlannerAgent 仅输出 JSON 规划，无需深度推理，降级到 `flash + disabled` 可节省 70%+ token 成本。不配置则复用 `llm_config`。

#### 5.2.3 多模态 LLM（`multimodal_llm_config`，可选）

浏览器 `visual_click` 视觉兜底用，需指向视觉模型（如 `qwen-vl-max` / `gpt-4o`）。不配置则视觉兜底降级为不可用，五级 DOM 容错仍完整。

#### 5.2.4 Agent 行为（`agent_config`）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_iterations` | `300` | Agent 最大迭代次数（与 4 小时超时联动） |
| `max_retries` | `3` | 工具调用最大重试 |
| `max_search_results` | `10` | 单次搜索最大结果数 |
| `session_timeout_seconds` | `14400` | 会话硬超时（4 小时），强制注入总结指令 |
| `session_warning_seconds` | `12000` | 软警告（200 分钟，83%），注入收敛提示 |
| `stream_final_answer` | `true` | 最终答案切片推送，降低感知时延 |
| `stream_chunk_min_chars` / `stream_chunk_max_chars` | `50` / `300` | 切片字符范围 |
| `stream_chunk_delay_ms` | `30` | 切片推送间隔 |
| `tool_filter_enabled` | `true` | 工具按需装配（按步骤关键词过滤） |
| `tool_filter_min_tools` | `3` | 过滤后低于此值回退全量 |

#### 5.2.5 记忆系统（`memory_config`）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `profile` | `balanced` | 预设：`balanced`（平衡）/ `lightweight`（延迟压缩）/ `heavy`（提前压缩） |
| `compression_strategy` | `auto` | `auto` / `proactive`（主动）/ `reactive`（被动） |

#### 5.2.6 MCP 工具（`mcp_config.mcpServers`）

```yaml
mcp_config:
  mcpServers:
    amap:                                    # 高德地图
      transport: streamable_http
      enabled: true
      url: https://mcp.amap.com/mcp?key=YOUR_KEY
    mcp-multimodal:                          # 多模态服务（docker-compose 内置）
      transport: streamable_http
      enabled: true
      url: http://mcp-multimodal:9100/mcp
    system:                                # 供应链系统（示例外部 MCP）
      transport: streamable_http
      enabled: true
      url: http://host.docker.internal:8080/mcp
```

> 支持的 transport：`streamable_http` / `stdio` / `sse`。

#### 5.2.7 A2A Agent（`a2a_config.a2a_servers`）

```yaml
a2a_config:
  a2a_servers: []        # A2A Agent 服务地址列表
```

#### 5.2.8 搜索（`search_config`）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `cache_enabled` | `true` | 搜索结果 Redis 缓存 |
| `cache_ttl_seconds` | `3600` | 缓存 TTL |
| `fetch_timeout` | `15` | 网页抓取超时 |
| `fetch_max_chars` | `10000` | 单页最大字符 |
| `fetch_max_concurrency` | `5` | 抓取并发 |
| `deep_research_config.max_depth` | `2` | 深度研究迭代层数 |
| `deep_research_config.time_limit_seconds` | `120` | 研究时间上限 |

#### 5.2.9 性能与缓存

| 配置块 | 作用 |
|--------|------|
| `tool_cache_config` | 幂等工具结果缓存（白名单：`web_search`/`deep_research`/`file_read`/`skill_list` + 部分 MCP 工具） |
| `tool_execution_config` | 工具并行执行（黑名单：`shell_*`/`browser_*`/`file_write` 等串行；`shell_execute` 按 `session_id` 隔离） |
| `idempotent_tool_dedup_config` | 幂等写操作去重（TTL 1 小时，防长会话重复发起） |
| `session_prompt_cache_config` | 会话级提示词缓存（L1 内存 + L2 Redis，TTL 对齐会话超时 4 小时） |

### 5.3 配置加载与缓存

[core/config.py](core/config.py) 使用 `@lru_cache` 缓存 `Settings` 实例，整个应用生命周期只读取一次 `.env`。`config.yaml` 由应用层服务按 `APP_CONFIG_FILEPATH` 读取。

---

## 六、API 路由总览

所有路由在 [app/interfaces/endpoints/routes.py](app/interfaces/endpoints/routes.py) 聚合，统一加前缀 `/api`。认证接口无需 Token，其余接口需 `Authorization: Bearer <access_token>`。

### 6.1 认证模块（`/api/auth`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 用户注册（username 3-50 / phone 11 位 / password 6-50） |
| POST | `/api/auth/login` | 登录，返回 access_token + refresh_token |
| POST | `/api/auth/logout` | 登出，Token 加入 Redis 黑名单 |
| POST | `/api/auth/refresh` | 刷新令牌 |
| POST | `/api/auth/change-password` | 修改密码 |
| GET | `/api/auth/me` | 获取当前用户信息 |

### 6.2 状态模块（`/api/status`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 系统健康检查（postgres/redis/fastapi/cos） |

### 6.3 应用配置（`/api/app-config`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET / POST | `/api/app-config` | 读取/更新应用配置（运行时） |

### 6.4 文件模块（`/api/files`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/files` | 文件上传 |
| GET | `/api/files/{id}/download` | 文件下载 |

### 6.5 会话模块（`/api/sessions`）

> F2-1 路由拆分：原大文件按职责拆为 5 个子路由，共享 `prefix=/sessions` 与 `tags=["会话模块"]`。

| 方法 | 路径 | 说明 | 文件 |
|------|------|------|------|
| POST | `/api/sessions` | 创建任务会话 | session_routes.py |
| GET | `/api/sessions` | 获取会话列表 | session_routes.py |
| POST | `/api/sessions/stream` | 流式获取会话列表（SSE，变更时推送） | session_routes.py |
| GET | `/api/sessions/{id}` | 获取会话详情（含事件历史） | session_routes.py |
| POST | `/api/sessions/{id}/delete` | 删除会话（级联清理沙箱） | session_routes.py |
| POST | `/api/sessions/{id}/stop` | 停止会话任务 | session_routes.py |
| POST | `/api/sessions/{id}/clear-unread-message-count` | 清除未读数 | session_routes.py |
| POST | `/api/sessions/{id}/chat` | 发起聊天（SSE 流式，支持 Last-Event-ID 断连恢复） | chat_routes.py |
| GET | `/api/sessions/{id}/files` | 读取会话文件 | session_file_routes.py |
| GET | `/api/sessions/{id}/shell` | 读取 Shell 输出 | session_shell_routes.py |
| WS | `/api/sessions/{id}/vnc` | VNC WebSocket 代理 | session_vnc_routes.py |

### 6.6 沙箱回调（内部端点）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/sandbox/...` | 沙箱异步任务完成回调（Docker 网络内调用） |

> 完整的请求/响应 Schema 见 [app/interfaces/schemas/](app/interfaces/schemas/)，或启动后访问 `http://localhost:8000/docs` 查看 OpenAPI 文档。

---

## 七、本地开发环境搭建

适用于不使用 Docker、直接在宿主机运行 API 进行开发调试的场景（需本机或 docker-compose 提供 PostgreSQL + Redis）。

### 7.1 前置依赖

| 要求 | 版本 | 说明 |
|------|------|------|
| Python | 3.12+ | `python --version` 验证 |
| uv | latest | `pip install uv`，依赖管理（比 pip 快 10-100 倍） |
| PostgreSQL | 15+ | 本机安装或用 docker-compose 的 postgres |
| Redis | 7.2+ | 本机安装或用 docker-compose 的 redis |
| Docker | 27+ | 沙箱动态创建需要（API 通过 Docker Socket 管理沙箱） |
| Playwright 浏览器 | - | `playwright install`（浏览器自动化需要） |

### 7.2 安装步骤

```bash
# 1.进入 api 目录
cd api

# 2.创建虚拟环境
python -m venv .venv

# 3.激活虚拟环境
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1

# 4.安装依赖（uv 方式，推荐）
uv pip install -r requirements.txt
# 或直接用 pyproject.toml:
uv sync

# 5.安装 Playwright 浏览器（浏览器自动化需要）
playwright install
```

> Windows 若遇到 `.venv\Scripts\Activate.ps1` 执行策略错误，先运行：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

### 7.3 配置环境变量

复制模板并修改：

```bash
cp .env.example .env
```

编辑 `.env`，**本地开发关键修改项**：

```bash
# 日志级别（开发建议 DEBUG）
LOG_LEVEL=DEBUG

# 数据库（若本机已装 PostgreSQL，保持 localhost；若用 docker-compose 的 postgres，改为 postgres）
SQLALCHEMY_DATABASE_URI=postgresql+asyncpg://postgres:postgres@localhost:5432/manus

# Redis（同上，localhost 或 redis）
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# 沙箱（本地开发可留空 SANDBOX_ADDRESS，由 API 动态创建；需 Docker 运行）
SANDBOX_IMAGE=sandbox:latest
SANDBOX_NETWORK=IDO-network

# OSS（本地开发可填测试地址或留空）
OSS_BASE_URL=
```

### 7.4 配置 LLM

编辑 [config.yaml](config.yaml)，填写真实的 LLM API Key：

```yaml
llm_config:
  provider: openai
  base_url: https://api.deepseek.com      # 或 https://open.bigmodel.cn/api/paas/v4 等
  api_key: sk-your-real-key               # 务必填写真实 Key
  model_name: deepseek-v4-flash
  # ...其余保持默认
```

### 7.5 准备依赖服务

**方式 A：仅启动 PostgreSQL + Redis（轻量）**

```bash
# 在项目根目录
docker compose up -d redis postgres
```

**方式 B：启动全部后端服务（含 MCP 多模态 / SearXNG / 沙箱镜像）**

```bash
# 在项目根目录
docker compose up -d --build
```

### 7.6 数据库迁移

本地开发若不通过容器启动 API（则 lifespan 不会自动迁移），需手动执行：

```bash
# 在 api 目录，激活虚拟环境后
alembic upgrade head
```

> 通过容器启动时无需手动迁移，`lifespan` 会自动执行 `alembic upgrade head`。

### 7.7 启动开发服务器

```bash
# 方式 A：开发脚本（--reload 热更新，推荐）
bash dev.sh
# 或 Windows:
# .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 方式 B：直接 uvicorn
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动成功后：

- API 服务：`http://localhost:8000`
- OpenAPI 文档：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/api/status`

### 7.8 验证开发环境

```bash
# 1.健康检查（应返回 postgres/redis/fastapi 状态）
curl http://localhost:8000/api/status

# 2.登录（默认 admin/admin123）
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 3.用返回的 access_token 创建会话
curl -X POST http://localhost:8000/api/sessions \
  -H "Authorization: Bearer <access_token>"
```

---

## 八、本地部署（Docker）

适用于本地完整运行后端全部服务（推荐使用项目根目录的一键部署脚本）。

### 8.1 一键部署（Windows，推荐）

在**项目根目录**执行：

```powershell
.\deploy.ps1
```

脚本自动完成（10 步）：
1. 检查 Docker 环境
2. 检查/创建 `.env`
3. 清理旧容器
4. 构建 Sandbox 镜像（Chromium + Python + VNC + LibreOffice，约 3.3GB）
5. 构建 MCP 多模态镜像
6. 构建 API 镜像
7. 启动全部服务（`docker compose up -d`）
8. 等待健康检查（30-60 秒）
9. 验证 MCP 多模态
10. 验证 SearXNG

> 首次构建沙箱镜像约 10-15 分钟，后续构建有缓存会快很多。

### 8.2 手动 Docker Compose 部署

在**项目根目录**执行：

```bash
# 构建并启动
docker compose up -d --build

# 查看状态
docker compose ps
```

### 8.3 预期状态

```
NAME              STATUS                   PORTS
api               Up (healthy)             0.0.0.0:8000->8000/tcp
mcp-multimodal    Up (healthy)             0.0.0.0:9100->9100/tcp
postgres          Up (healthy)             5432/tcp
redis             Up (healthy)             6379/tcp
searxng           Up (healthy)             8080/tcp
```

> **sandbox 容器状态为 `Exited` 是正常的**：docker-compose 中的 sandbox 服务仅用于构建 `sandbox:latest` 镜像，实际沙箱容器由 API 在用户发起会话时通过 Docker SDK 按需创建。

### 8.4 部署后验证

```bash
# API 健康检查
curl http://localhost:8000/api/status

# MCP 多模态健康检查
curl http://localhost:9100/health

# SearXNG（容器内访问）
docker compose exec searxng wget -qO- http://127.0.0.1:8080/healthz

# OpenAPI 文档
# 浏览器打开 http://localhost:8000/docs
```

### 8.5 修改配置后生效

| 修改内容 | 生效方式 |
|---------|---------|
| `.env` 环境变量 | `docker compose up -d`（重新创建容器） |
| `config.yaml` 应用配置 | `docker compose build api && docker compose up -d api`（重新构建镜像） |
| 代码修改 | `docker compose build api && docker compose up -d api` |

---

## 九、数据库迁移

迁移由 [Alembic](https://alembic.sqlalchemy.org/) 管理，脚本位于 [alembic/versions/](alembic/versions/)，配置见 [alembic.ini](alembic.ini)。

### 9.1 自动迁移

**容器部署**：API 容器启动时 `lifespan` 自动执行 `alembic upgrade head`，无需手动操作。

**本地开发**：若直接 `uvicorn` 启动，lifespan 同样会自动迁移；若只想单独执行迁移：

```bash
# 激活虚拟环境后，在 api 目录
alembic upgrade head
```

### 9.2 常用迁移命令

```bash
# 执行迁移到最新版本
alembic upgrade head

# 回滚一个版本
alembic downgrade -1

# 查看当前版本
alembic current

# 查看历史
alembic history

# 生成新迁移脚本（修改 ORM 模型后）
alembic revision --autogenerate -m "描述变更内容"
```

### 9.3 容器内执行迁移

```bash
# 手动执行迁移
docker compose exec api alembic upgrade head

# 生成迁移脚本
docker compose exec api alembic revision --autogenerate -m "add new table"
```

### 9.4 数据库备份与恢复

```bash
# 备份
docker compose exec postgres pg_dump -U postgres manus > backup.sql

# 恢复
docker compose exec -T postgres psql -U postgres manus < backup.sql
```

### 9.5 现有迁移版本

| 版本 | 说明 |
|------|------|
| `87ed1cbb1088` | create sessions table |
| `0e0d242438bc` | create files table |
| `b2c3d4e5f6a7` | create users table |
| `a1b2c3d4e5f6` | add user_id to sessions |
| `c3d4e5f6a7b8` | add phone column to users |
| `d4e5f6a7b8c9` | add sync_status to files |

> **注意事项**：
> - `alembic.ini` 中 `sqlalchemy.url` 为同步驱动（`psycopg2`），应用运行时用异步驱动（`asyncpg`），两者通过 `env.py` 分别处理。
> - 生成迁移脚本后**务必检查** `upgrade()` / `downgrade()` 内容，autogenerate 可能遗漏约束或索引。
> - `alembic.ini` 的 `[logger_root] level=WARNING` 会覆盖应用日志级别，因此 `main.py` 在迁移完成后会重新初始化日志。

---

## 十、测试

### 10.1 单元测试

配置见 [pytest.ini](pytest.ini)：

```bash
# 在 api 目录，激活虚拟环境后
pytest

# 运行指定测试文件
pytest tests/test_xxx.py

# 查看详细输出
pytest -v -s
```

测试约定：
- 测试文件：`test_*.py` 或 `*_test.py`
- 测试类：`Test*`
- 测试函数：`test_*`
- 测试目录：`tests/`
- 缓存：`tmp/.pytest_cache`

### 10.2 E2E 测试

项目根目录提供 [_e2e_test.py](_e2e_test.py) 端到端测试脚本，启动服务后可运行验证完整链路。

---

## 十一、问题调试手册

### 11.1 日志查看

**容器部署**：

```bash
# 查看所有服务日志
docker compose logs -f

# 查看 API 日志
docker compose logs -f api

# 查看最近 100 行
docker compose logs --tail 100 api
```

**本地开发**：`uvicorn --reload` 启动时日志直接输出到终端。日志级别由 `.env` 的 `LOG_LEVEL` 控制（开发建议 `DEBUG`）。

### 11.2 常见问题排查

#### Q1: API 启动后一直重启 / 无法健康检查通过

1. **检查依赖服务**：`docker compose ps`，确认 `postgres` / `redis` 为 `healthy`
2. **检查数据库连接串**：`.env` 的 `SQLALCHEMY_DATABASE_URI`，Docker 部署主机应为 `postgres`，库名 `manus`，密码与 `POSTGRES_PASSWORD` 一致
3. **检查 LLM 配置**：`config.yaml` 的 `api_key` 是否有效
4. **查看启动日志**：`docker compose logs api`，关注 `alembic upgrade` 是否成功、`_seed_default_admin` 是否成功

#### Q2: 沙箱容器创建失败

1. **检查 Docker Socket 挂载**：docker-compose.yml 中 `api` 服务挂载了 `/var/run/docker.sock`
2. **检查沙箱镜像**：`docker images | grep sandbox`，确认 `sandbox:latest` 已构建
3. **检查网络**：`docker network ls | grep IDO`，确认 `IDO-network` 存在
4. **检查 SANDBOX_NETWORK**：`.env` 的 `SANDBOX_NETWORK=IDO-network` 须与 docker-compose 网络名一致

#### Q3: LLM 调用报 503 Service is too busy

系统已内置 503 容错（指数退避重试，默认 5 次）。如仍频繁失败：

1. 调大 `config.yaml` 的 `llm_config.max_retries`（1-10）
2. 切换备用 LLM 服务商（修改 `base_url` + `api_key` + `model_name`）
3. 检查 LLM 服务商状态页

#### Q4: 搜索功能不工作

1. 检查 SearXNG 容器健康：`docker compose ps searxng`
2. 国内服务器配置代理：`.env` 设置 `SANDBOX_HTTPS_PROXY`
3. 系统会自动降级到 Bing 搜索作为备用

#### Q5: 数据库迁移失败

1. 查看日志：`docker compose logs api | grep alembic`
2. 确认数据库可达且库 `manus` 已创建（postgres 容器会自动创建）
3. 手动执行迁移查看详细错误：`docker compose exec api alembic upgrade head`
4. 若迁移版本冲突，查看当前版本：`alembic current`

#### Q6: VNC 远程桌面无法连接

1. 确认会话已创建沙箱（聊天后触发）
2. 检查 WebSocket 路径：`/api/sessions/{id}/vnc`
3. 查看沙箱 VNC 状态：日志中 `vnc_status_tracker` 相关输出
4. 确认沙箱镜像含 VNC 服务（Xvfb + x11vnc + websockify，端口 5900）

#### Q7: SSE 流式响应中断

1. 系统支持 `Last-Event-ID` 断连恢复，前端 EventSource 会自动携带
2. 检查 Nginx（生产）是否关闭缓冲：`proxy_buffering off`
3. 检查 `proxy_read_timeout`（生产配置 86400s）

#### Q8: Playwright 浏览器自动化失败

1. 本地开发需执行 `playwright install` 安装浏览器
2. 容器内沙箱已预装 Chromium
3. 视觉点击（`visual_click`）需配置 `multimodal_llm_config`，否则降级为 DOM 容错

### 11.3 调试技巧

#### 进入容器调试

```bash
# 进入 API 容器
docker compose exec api bash

# 进入后可执行：
python -c "from core.config import get_settings; print(get_settings())"
alembic current
python -c "from app.infrastructure.storage.redis import get_redis; import asyncio; asyncio.run(get_redis().init())"
```

#### 临时修改日志级别

修改 `.env` 的 `LOG_LEVEL=DEBUG` 后重启：`docker compose restart api`。

#### 查看 Redis 数据

```bash
docker compose exec redis redis-cli
# 查看所有键
KEYS *
# 查看会话事件流
XRANGE <stream_key> - +
```

#### 查看数据库

```bash
docker compose exec postgres psql -U postgres manus
# 查看表
\dt
# 查看会话
SELECT id, title, status, user_id FROM sessions LIMIT 10;
```

---

## 十二、二次开发指引

### 12.1 新增 API 端点

**示例：新增一个"收藏"模块**

1. **创建路由文件** [app/interfaces/endpoints/favorite_routes.py](app/interfaces/endpoints/favorite_routes.py)：

```python
from fastapi import APIRouter, Depends
from app.core.security import get_current_user_id
from app.interfaces.schemas import Response
from app.interfaces.schemas.favorite import FavoriteRequest, FavoriteResponse  # 自定义 Schema

router = APIRouter(prefix="/favorites", tags=["收藏模块"])

@router.get("", response_model=Response[list[FavoriteResponse]], summary="获取收藏列表")
async def list_favorites(
    current_user_id: str = Depends(get_current_user_id),
    # favorite_service: FavoriteService = Depends(get_favorite_service),  # 注入应用服务
) -> Response[list[FavoriteResponse]]:
    # 调用应用层服务
    return Response.success(msg="获取成功", data=[])
```

2. **在路由聚合器注册** [app/interfaces/endpoints/routes.py](app/interfaces/endpoints/routes.py)：

```python
from . import (..., favorite_routes)

def create_api_routes() -> APIRouter:
    api_router = APIRouter()
    # ...原有路由
    api_router.include_router(favorite_routes.router)
    return api_router
```

3. **创建 Schema** [app/interfaces/schemas/favorite.py](app/interfaces/schemas/favorite.py)：

```python
from pydantic import BaseModel

class FavoriteRequest(BaseModel):
    session_id: str

class FavoriteResponse(BaseModel):
    id: str
    session_id: str
```

> 遵循现有模式：路由只做协议适配，业务逻辑放应用层服务，数据访问走仓储。

### 12.2 新增领域模型与数据库表

1. **创建领域模型** [app/domain/models/favorite.py](app/domain/models/favorite.py)：纯数据结构（Pydantic / dataclass）
2. **创建 ORM 模型** [app/infrastructure/models/favorite.py](app/infrastructure/models/favorite.py)：

```python
from sqlalchemy import Column, String, ForeignKey, DateTime
from .base import Base

class FavoriteORM(Base):
    __tablename__ = "favorites"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
```

3. **在** [app/infrastructure/models/__init__.py](app/infrastructure/models/__init__.py) **导出** ORM 模型（确保 Alembic 能发现）
4. **生成迁移**：`alembic revision --autogenerate -m "create favorites table"`
5. **检查迁移脚本**，执行：`alembic upgrade head`
6. **创建仓储接口与实现**：`domain/repositories/` + `infrastructure/repositories/`
7. **在 UoW 注册**：[domain/repositories/uow.py](app/domain/repositories/uow.py) 与 [infrastructure/repositories/db_uow.py](app/infrastructure/repositories/db_uow.py)

### 12.3 新增 Agent 工具

工具是 ReActAgent 可调用的能力单元。**示例：新增"翻译"工具**

1. **创建工具** [app/domain/services/tools/translate.py](app/domain/services/tools/translate.py)：

```python
from .base import BaseTool, ToolResult

class TranslateTool(BaseTool):
    @property
    def name(self) -> str:
        return "translate"

    @property
    def description(self) -> str:
        return "将文本翻译为指定语言。参数: text(待翻译文本), target_language(目标语言)"

    async def execute(self, text: str, target_language: str, **kwargs) -> ToolResult:
        # 调用 LLM 或外部翻译 API
        result = await self._llm.translate(text, target_language)
        return ToolResult(success=True, data={"translated": result})
```

2. **在工具注册表注册**：参考 [app/domain/services/tools/__init__.py](app/domain/services/tools/__init__.py) 现有模式
3. **（可选）加入工具缓存白名单**：若工具幂等（相同参数相同结果），在 `config.yaml` 的 `tool_cache_config.cacheable_tools` 添加工具名
4. **（可选）配置并行策略**：若工具有状态，在 `tool_execution_config.stateful_tool_names` 添加

### 12.4 新增 MCP 工具服务

在 [config.yaml](config.yaml) 的 `mcp_config.mcpServers` 下添加：

```yaml
mcp_config:
  mcpServers:
    your-mcp:
      transport: streamable_http      # 或 stdio / sse
      enabled: true
      description: "你的 MCP 服务描述"
      url: http://your-service:port/mcp
      # 若 stdio 模式:
      # command: python
      # args: ["-m", "your_mcp_server"]
      # env:
      #   API_KEY: xxx
```

修改后需重新构建 API 镜像：`docker compose build api`。

### 12.5 新增 A2A Agent

在 [config.yaml](config.yaml) 的 `a2a_config.a2a_servers` 添加 Agent 服务地址：

```yaml
a2a_config:
  a2a_servers:
    - http://your-agent-host:port
```

### 12.6 更换 LLM 模型

编辑 [config.yaml](config.yaml) 的 `llm_config`，任何兼容 OpenAI API 的服务均可接入：

| 服务商 | base_url | 示例 model_name |
|--------|----------|-----------------|
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.2` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| 本地 Ollama | `http://localhost:11434/v1` | `llama3` |

### 12.7 开发规范

遵循现有代码风格（工业级生产标准）：

- **命名**：语义化命名，文件小写下划线，类 PascalCase，函数/变量 snake_case
- **类型**：全面使用类型注解（Python 3.12+ 语法，如 `str | None`）
- **异步**：IO 操作一律 `async/await`，禁用阻塞调用
- **异常处理**：业务异常用 `app/application/errors/exceptions.py` 中定义的类，全局异常处理器统一捕获
- **日志**：`logging.getLogger(__name__)`，关键路径打 INFO，调试打 DEBUG
- **依赖注入**：通过 `app/interfaces/service_dependencies.py` 集中管理，便于测试 Mock
- **事务**：通过 Unit of Work（`get_uow()`）管理事务边界
- **注释**：复杂逻辑必须注释，文件头保留 `@File` 注释，关键函数写 docstring
- **路由拆分**：单个路由文件超过 ~300 行时按职责拆分（参考 F2-1 会话路由拆分）

---

## 十三、常用命令速查

### 本地开发

```bash
# 进入 api 目录
cd api

# 激活虚拟环境
source .venv/bin/activate          # Linux/macOS
.venv\Scripts\Activate.ps1         # Windows PowerShell

# 安装依赖
uv pip install -r requirements.txt
# 或
uv sync

# 安装 Playwright 浏览器
playwright install

# 启动开发服务器（热更新）
bash dev.sh
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 数据库迁移
alembic upgrade head
alembic revision --autogenerate -m "描述"
alembic downgrade -1

# 运行测试
pytest
pytest tests/test_xxx.py -v
```

### Docker 部署（在项目根目录）

```bash
# 一键部署（Windows）
.\deploy.ps1

# 手动部署
docker compose up -d --build
docker compose ps
docker compose logs -f api
docker compose restart api
docker compose down
docker compose down -v                # 清除数据卷（慎用，删数据）

# 重新构建 API（修改 config.yaml 或代码后）
docker compose build api && docker compose up -d api
```

### 沙箱管理

```bash
# 查看运行中的沙箱
docker ps --filter "name=sandbox"

# 手动销毁沙箱
docker rm -f sandbox-<8位UUID>

# 查看沙箱镜像
docker images | grep sandbox
```

### 数据库

```bash
# 进入 psql
docker compose exec postgres psql -U postgres manus

# 备份
docker compose exec postgres pg_dump -U postgres manus > backup.sql

# 恢复
docker compose exec -T postgres psql -U postgres manus < backup.sql

# 手动迁移
docker compose exec api alembic upgrade head
```

### 调试

```bash
# 进入 API 容器
docker compose exec api bash

# 查看配置
docker compose exec api python -c "from core.config import get_settings; print(get_settings())"

# 查看 Redis
docker compose exec redis redis-cli

# 查看当前日志级别
docker compose exec api python -c "import logging; print(logging.getLogger().level)"
```

### 服务地址（本地部署）

| 服务 | 地址 | 说明 |
|------|------|------|
| API | `http://localhost:8000` | FastAPI 后端 |
| OpenAPI 文档 | `http://localhost:8000/docs` | Swagger UI |
| MCP 多模态 | `http://localhost:9100/health` | 健康检查 |
| SearXNG | 容器内 `searxng:8080` | 未暴露到宿主机 |
| PostgreSQL | 容器内 `postgres:5432` | 库名 `manus` |
| Redis | 容器内 `redis:6379` | - |
| 默认账号 | `admin / admin123` | 启动时自动播种 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [根目录 README.md](../README.md) | 系统完整架构、并发承载力评估、生产部署、技术栈总览 |
| [环境部署.md](../环境部署.md) | 部署快速参考（若依规范），含 Nginx 配置与运维命令 |
| [config.yaml](config.yaml) | 应用配置（LLM/MCP/A2A/搜索/Agent/缓存） |
| [.env.example](.env.example) | 环境变量模板 |
| [pyproject.toml](pyproject.toml) | 依赖定义 |
| [OpenAPI 文档](http://localhost:8000/docs) | 启动服务后访问，交互式 API 文档 |
