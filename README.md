# Reddit 产品反馈洞察 Agent

给定一个产品类目（例如 "wireless earbuds"），Agent 会在 Reddit 上运行一个 ReAct 循环：

1. **思考（Thought）**：决定下一步搜索什么。
2. **行动（Action）**：搜索（数据来源可插拔，见下）。
3. **观察（Observation）**：用 LLM（或规则兜底）筛选出真正相关的帖子/评论，并打上痛点/功能诉求/竞品对比/好评等标签。
4. **判断是否充分（Sufficiency check）**：信息不够就回到第 1 步继续搜索；信息足够（或达到轮次上限/连续两轮无新证据）就进入总结阶段。
5. **总结（Summary）**：生成一份商家可读的产品优化报告。

前端（React + Vite）可以实时查看 Agent 的推理过程，并在完成后查看报告。

架构参考了 `demo/super_crawler`（一个更完整的 Reddit 需求发现系统），但简化为单一 ReAct 循环，并采用独立的 React 前端 + FastAPI 后端。

### 数据采集层是可插拔的

`react_agent.py` 只依赖 `app/collectors/base.py` 里定义的抽象接口 `Collector`（`available()` + `search(query, subreddit, limit)`），完全不知道、也不导入任何具体数据源。数据源是 Reddit（PRAW）、JSON 上传、还是以后的 Amazon/YouTube，对 ReAct 循环和后续的分析/打分/总结逻辑来说没有任何区别。

```
app/collectors/
  base.py          Collector 抽象接口 + CollectorContext
  registry.py       DataSource -> 工厂函数 的注册表，build_collector() 是唯一的查找入口
  reddit.py         RedditCollector（主数据源，PRAW，只读模式），文件底部 register_collector(...) 自注册
  json_upload.py    JsonUploadCollector（回退数据源），同样自注册
```

`run_manager.py` 启动一次调研时，只调用 `build_collector(CollectorContext(run, storage))`——它从注册表里查工厂函数并调用，从不对 `data_source` 做 if/else 分支。这意味着以后要接入 Amazon、YouTube 或其他评论平台：

1. 在 `models.py` 的 `DataSource` 里加一个新枚举值。
2. 新建 `app/collectors/amazon.py`，写一个实现 `Collector` 接口的类，并在文件底部调用 `register_collector(DataSource.AMAZON, ...)` 自注册。
3. 在 `app/collectors/__init__.py` 里加一行 `from . import amazon as _amazon`。

`react_agent.py`、`run_manager.py` 都不需要动。

现有两个数据源：

- **Reddit API**（主数据源，`app/collectors/reddit.py`）：实时抓取。**目前 Reddit Data API 走的是新的 "Responsible Builder" 审批流程，申请可能被拒或长时间挂起**，所以这条路径可能暂时用不了。
- **JSON 上传**（回退数据源，`app/collectors/json_upload.py`）：把预先准备好的 Reddit 帖子/评论 JSON 数组喂给 Agent，不需要任何 Reddit 凭证，适合演示、离线分析、或者 Reddit API 申请下来之前先把整套链路跑通。

新建调研时在前端选择「数据来源」即可切换，两者共用同一套后续分析/判断/总结逻辑。

## 目录结构

```
backend/    FastAPI + SQLite + ReAct agent（Python）
frontend/   Vite + React + TypeScript
```

## 准备工作

### 1. Reddit API 凭证（可选——数据来源选「JSON 上传」时不需要）

1. 访问 https://www.reddit.com/prefs/apps
2. 点击 "create app" / "create another app"
3. 类型选择 **script**
4. 记下 `client_id`（应用名下方那串字符）和 `client_secret`

如果暂时申请不到（见上面「数据来源是可插拔的」），直接跳过这一步，新建调研时选择「JSON 上传」即可。

### 2. DeepSeek API Key（可选，但强烈建议）

在 https://platform.deepseek.com 获取。不配置也能跑，Agent 会退化为基于关键词规则的兜底逻辑（结果质量明显更弱）。

### 3. 配置 `.env`

```bash
cd backend
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY / REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT
# 如果只打算用 JSON 上传模式，这一步可以直接跳过
```

## 运行后端

```bash
cd backend
python -m venv .venv
./.venv/Scripts/pip install -e .          # Windows
# source .venv/bin/activate && pip install -e .   # macOS/Linux
./.venv/Scripts/python -m uvicorn app.main:app --reload
```

后端默认监听 `http://127.0.0.1:8000`。

运行测试（不需要任何 API key，使用注入的假 collector/LLM 客户端）：

```bash
./.venv/Scripts/python -m pytest
```

## 运行前端

```bash
cd frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。

## 使用流程

1. 在首页点击「新建调研」，填写产品类目（必填）、关键词、目标 subreddit、最大搜索轮次、目标证据数量。
2. 选择「数据来源」：
   - **Reddit API**：需要 `.env` 里配置好 Reddit 凭证。如果没配置，页面会提示警告并建议改用 JSON 上传。
   - **JSON 上传**：上传一个 Reddit 帖子/评论 JSON 数组文件（页面上有格式示例），不需要任何 Reddit 凭证。
3. 提交后进入调研详情页，页面每 2 秒轮询一次后端，实时展示 Agent 的思考 / 搜索 / 观察 / 充分性判断 每一步。
4. 当状态变为「已完成」后，点击「查看商家报告」，查看按方面聚合的痛点、功能诉求、好评、竞品对比、情感分布，以及建议的产品优化行动。

## 说明

- 数据来源选「Reddit API」但没有配置凭证时，搜索行动会失败并记录在推理轨迹中（不会导致整个 Agent 崩溃），Agent 仍会走到轮次上限并生成一份基于 0 条证据的报告——用于验证整套链路，但没有实际意义。想看真实内容，要么配置 Reddit 凭证，要么改用「JSON 上传」。
- 「JSON 上传」模式下，Agent 不会重复返回同一条数据；上传的数据用完后视为「连续两轮无新证据」，自动进入总结阶段——这也是验证 ReAct 循环判断逻辑最简单可靠的方式。
- 没有配置 DeepSeek key 时，规划 / 分析 / 充分性判断 / 总结 全部退化为确定性的关键词规则，可用于本地开发和测试，但报告质量远不如接入真实 LLM。
