# 智能采集调度平台

面向多源网页采集、增量抓取、自动化流程编排、采集结果校验与失败重试的智能数据采集调度平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集引擎 | `collector` | 采集任务 | 任务编号、目标网址、HTTP方法 |
| 页面抽取规则 | `crawlrule` | 抽取规则 | 规则编号、入口地址、列表选择器 |
| 内容解析器 | `parser` | 解析模板 | 模板编号、目标字段名、XPath表达式 |
| 增量采集 | `increment` | 增量游标 | 游标编号、来源标识、增量字段 |
| 限速调度 | `ratelimit` | 限速策略 | 策略编号、目标域名、并发上限 |
| 失败重试 | `retry` | 重试策略 | 策略编号、失败类型、最大重试次数 |
| 自动化流程 | `rpaflow` | 流程编排 | 流程编号、入口URL、页面定位器 |
| 验证码识别 | `captcha` | 识别方案 | 方案编号、验证码类型、识别服务地址 |
| 代理池管理 | `proxy` | 代理节点 | 节点编号、代理地址、代理协议 |
| 会话状态 | `session` | 会话配置 | 配置编号、登录网址、Cookie策略 |
| 链路分发 | `router` | 分发规则 | 规则编号、来源URL模式、目标引擎 |
| 采集校验 | `validate` | 校验规则 | 规则编号、校验目标字段、校验表达式 |
| 去重过滤 | `dedup` | 去重策略 | 策略编号、业务键列、相似度算法 |
| 格式归一 | `format` | 转换规则 | 规则编号、输入字段、输出字段 |
| 采集流水线 | `pipeline` | 流水线编排 | 流水线编号、阶段顺序、阶段名称 |
| 采集存储 | `storage` | 存储配置 | 配置编号、存储类型、连接地址 |
| 采集监控 | `monitor` | 监控指标 | 指标编号、监控目标、采集频率 |
| 定时调度 | `scheduler` | 调度计划 | 计划编号、Cron表达式、关联任务 |

## 离线样本试验（采集校验 × 内容解析器）

运维只能看登记文本的问题由「离线样本试验面板」解决，链路全部只读正式数据：

- **校验页面板**（`/validate`）：选择规则或直接粘贴表达式，填入 JSON 样本可反复试验；
  返回字段级失败解释（字段存在性、求值、容差判断三阶段）与采用的规则版本/语言版本。
- **解析器页送样**（`/parser`）：同一份解析样本先调 `/api/parser/sample-handoff`
  做只读校验，再经浏览器 sessionStorage 带到校验页（不进 URL/服务端日志），
  试验结果会标记来源模板。
- **安全表达式**：`app/services/expression_sandbox.py` 用 AST 白名单 + 空内置
  命名空间求值，禁止 import、赋值、lambda/推导式、dunder 属性和白名单外函数，
  不能执行任意 Python；另有步数、序列展开与样本体量预算。
- **编译缓存**：缓存身份为 `sha256(语言版本 | 规则版本 | 表达式原文)`，
  有界 LRU（默认 64）+ 负缓存（历史不合法表达式只产出失败解释，不污染正式记录）。
  缓存条目只含编译产物，样本永不进入共享缓存；规则改版换版本后旧编译结果不可能
  混入新结论；淘汰后重新编译，结论与缓存身份保持一致。
- **只读保证**：试验不修改解析模板或正式校验记录；规则修订走
  `POST /api/validate/{id}/revision`，自动递增「规则版本」并保留有界修订记录。
- **可观测接口**：`GET/DELETE /api/validate/cache` 查看/清空缓存计数，
  `GET/DELETE /api/validate/trials/history` 查看/清空有界试验历史。

冷/热计数复现与验证：

```bash
make test    # 后端 28 项 unittest（安全限制、缓存身份/淘汰、版本与并发隔离）+ 前端类型检查
make bench   # 每轮固定 1 次编译未命中 + 19 次缓存命中，可重复复现
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
