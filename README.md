# PatentWriterAgent - 专利写作多智能体系统

基于子代理（subagent）架构，将技术交底书（.docx）自动转换为符合中国《专利法》规范的完整专利申请文件。

## 特性

- 8 阶段流水线：文档解析 → 专利检索 → 大纲生成 → 摘要撰写 → 权利要求书 → 说明书 → 图表生成 → 文档合并
- 内置 Streamlit Web 控制台：上传文件、实时日志、会话管理、产物预览/下载
- Fast 模式：仅输入简要发明构思，自动扩写为技术交底书后继续生成专利文稿
- 具体实施方式自动生成 >10000 字，实施例 ≤3 个
- 方法+装置+设备+介质四类独立权利要求全覆盖
- Mermaid 格式专利附图自动生成
- 不依赖外部 AI CLI，直接通过 Python SDK 调用模型 API

## 快速开始

### 方式一：Web 前端模式（推荐）

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 配置模型 API（任选一种后端）
# Anthropic-compatible
export ANTHROPIC_API_KEY="your-key"
# 或 Anthropic 兼容网关（如智谱等）可配合：
# export ANTHROPIC_BASE_URL="https://..."
# export ANTHROPIC_MODEL="..."

# OpenAI-compatible
# export OPENAI_API_KEY="your-key"
# export OPENAI_BASE_URL="https://..."
# export OPENAI_MODEL="..."

# 3) 启动
python run_app.py
# 或
streamlit run patent_writer_app.py
```

启动后访问 [http://localhost:8501](http://localhost:8501)。

支持两种输入模式：
- Normal：上传 `docx` 技术交底书后生成专利文稿
- Fast：输入简要发明构思，系统先自动扩写为结构化交底 `docx`，再进入标准流水线生成专利文稿

侧边栏可切换 Runtime backend（Anthropic-compatible / OpenAI-compatible）。

### 方式二：命令行直接运行流水线（无 CLI）

```bash
python pipeline_runner.py \
  --session-id 11111111-2222-3333-4444-555555555555 \
  --input-path data/输入.docx \
  --runtime-backend anthropic
```

输出文件位于：`output/temp_[session-id]/06_final/complete_patent.md`。

### 方式三：Skill 模式

将 `patent-writer/` 目录复制到 `~/.claude/skills/` 下，或安装 `patent-writer.skill` 文件，即可在任意项目中通过自然语言触发专利写作：

```
帮我根据这份技术交底书撰写专利申请
```

### 方式四：Docker

```bash
docker build -t patent-writer .
docker run -p 8009:8009 patent-writer
```

## 配置说明

### 模型配置（必须）

项目支持两类后端：
- `anthropic`：`ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`
- `openai`：`OPENAI_API_KEY`

可选配置：
- `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`
- `ANTHROPIC_MODEL` / `OPENAI_MODEL`
- `PATENT_RUNTIME_BACKEND`（默认后端）

兼容配置：如果存在 `.claude/settings.local.json`，其中 `env` 字段会自动加载到进程环境（仅在同名环境变量未设置时生效）。

### MCP 工具（可选）

编辑 `.mcp.json`，填入以下 API KEY：
- `SERPAPI_API_KEY`：用于 Google Patents 专利检索
- `EXA_API_KEY`：用于 Exa 技术文献搜索

当前无 CLI 运行时下，检索阶段可在无 MCP 时降级为“检索建议+写作风格参考”。

## 架构设计

### 子代理流水线（严格顺序执行）

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

| 子代理 | 职责 | 输出 |
|--------|------|------|
| input-parser | 解析 docx 技术交底书 | `01_input/parsed_info.json` |
| patent-searcher | 生成相似专利与现有技术分析（可降级） | `02_research/similar_patents.json` |
| outline-generator | 设计专利大纲 | `03_outline/patent_outline.md` |
| abstract-writer | 撰写说明书摘要（≤300字） | `04_content/abstract.md` |
| claims-writer | 撰写权利要求书 | `04_content/claims.md` |
| description-writer | 撰写说明书（>10000字） | `04_content/description.md` |
| diagram-generator | 生成 Mermaid 附图 | `05_diagrams/**/*.mmd` |
| markdown-merger | 合并为完整专利文件 | `06_final/complete_patent.md` |

## 输出目录结构

每次执行创建 `output/temp_[uuid]/`：

```
output/temp_[uuid]/
├── 01_input/          # 解析后的结构化数据
├── 02_research/       # 专利检索/分析结果
├── 03_outline/        # 专利大纲
├── 04_content/        # 摘要、权利要求书、说明书
├── 05_diagrams/       # Mermaid 图表
└── 06_final/          # 最终合并的完整专利文件
```

## 关键文件

| 文件 | 说明 |
|------|------|
| `pipeline_runner.py` | 无 CLI 的 8 阶段流水线执行器 |
| `runtime_client.py` | 运行时后端适配（Anthropic/OpenAI） |
| `patent_writer_app.py` | Web UI |
| `run_app.py` | 启动脚本与环境检查 |
| `.claude/agents/*.md` | 8 个子代理定义 |
| `PATENT_SKILL.md` | 专利撰写规范指南 |
| `patent-writer/` | 封装后的 Skill 目录 |

## 许可证

[AGPL-3.0](LICENSE)
