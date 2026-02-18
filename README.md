# PatentWriterAgent - 专利写作多智能体系统

基于子代理（subagent）架构，将技术交底书（.docx）自动转换为符合中国《专利法》规范的完整专利申请文件。

## 特性

- 8 阶段流水线：文档解析 → 专利检索 → 大纲生成 → 摘要撰写 → 权利要求书 → 说明书 → 图表生成 → 文档合并
- 内置 Streamlit Web 控制台：上传文件、实时日志、会话管理、产物预览/下载
- Fast 模式：仅输入简要发明构思，自动扩写为技术交底书后继续生成专利文稿
- 单分支双运行时：同一代码同时支持 `CLI` 与 `Native SDK`

## 运行方式

### 1) Web 控制台（推荐）

```bash
pip install -r requirements.txt
python run_app.py
# 或 streamlit run patent_writer_app.py
```

启动后访问 [http://localhost:8501](http://localhost:8501)。
在侧边栏可选择：
- Execution mode: `CLI runtime` 或 `Native runtime`
- Backend: CLI(`claude/codex/gemini`) 或 Native(`anthropic/openai`)

### 2) CLI runtime（兼容旧模式）

安装任一 CLI：

```bash
npm install -g @anthropic-ai/claude-code
# 或 npm install -g @openai/codex
# 或 npm install -g @google/gemini-cli
```

在 Web 里选择 `CLI runtime` 后可直接执行。

### 3) Native runtime（无 CLI）

配置至少一个 API 后端：

```bash
export ANTHROPIC_API_KEY="your-key"
# 或
# export OPENAI_API_KEY="your-key"
```

命令行直跑流水线：

```bash
python pipeline_runner.py \
  --session-id 11111111-2222-3333-4444-555555555555 \
  --input-path data/输入.docx \
  --runtime-backend anthropic \
  --description-parallelism 2
```

## 配置说明

Native runtime 可选环境变量：
- `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`
- `ANTHROPIC_MODEL` / `OPENAI_MODEL`
- `PATENT_RUNTIME_BACKEND`（默认 native 后端）
- `PATENT_DESCRIPTION_PARALLELISM`（说明书章节并发生成数，默认 2，范围 1-6）

兼容配置：如果存在 `.claude/settings.local.json`，其中 `env` 字段会自动加载（同名环境变量优先）。

## 关键文件

- `patent_writer_app.py`：Web UI（双运行时路由）
- `run_app.py`：启动与环境检查（同时检查 CLI + Native）
- `pipeline_runner.py`：Native 执行器（8 阶段）
- `runtime_client.py`：Native 模型后端适配
- `.claude/agents/*.md`：8 个子代理定义

## 许可证

[AGPL-3.0](LICENSE)
