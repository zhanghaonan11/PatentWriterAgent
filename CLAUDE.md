# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

PatentWriterAgent 是专利写作多智能体系统，将技术交底书（.docx）自动转换为符合中国《专利法》规范的完整专利申请文件。采用**单分支双运行时架构**，同一套代码支持 CLI runtime（claude/codex/gemini）和 Native runtime（Anthropic/OpenAI Python SDK）。

## 开发命令

```bash
pip install -r requirements.txt    # 安装依赖
python run_app.py --check-only     # 仅检查环境（CLI 可用性 + Native 后端 + 依赖）
python run_app.py                  # 启动 Streamlit Web UI（默认 http://localhost:8501）
streamlit run patent_writer_app.py # 直接启动（跳过环境检查）
```

直接运行 Native 流水线（无 Web UI）：
```bash
python pipeline_runner.py \
  --session-id <uuid> \
  --input-path data/输入.docx \
  --runtime-backend anthropic    # 或 openai
```

Docker 部署：
```bash
docker build -t patent-writer . && docker run -p 8009:8009 patent-writer
```

无测试套件、无 lint 配置、无 CI/CD。

## 架构：双运行时路由

核心入口 `patent_writer_app.py`（Streamlit Web UI）根据 execution mode 分流：

```
patent_writer_app.py (Web UI)
├── CLI runtime → subprocess.Popen → claude/codex/gemini CLI
│   CLI 读取 .claude/agents/*.md 作为子代理定义
│   MCP 工具（google-patents, exa）仅在此模式可用
└── Native runtime → subprocess.Popen → pipeline_runner.py
    pipeline_runner.py 读取 patent-writer/references/ 下的代理定义和写作指南
    通过 runtime_client.py 统一调用 Anthropic/OpenAI SDK
```

**关键设计**：两种模式在 UI 层都通过子进程启动，保持一致的异步执行和日志流式输出。

## 四大核心文件职责

| 文件 | 职责 | 行数 |
|------|------|------|
| `patent_writer_app.py` | Streamlit Web UI：会话管理、进程管理、产物预览下载 | ~1600 |
| `pipeline_runner.py` | Native 模式 8 阶段执行器，每阶段含 prompt 构建和输出验证 | ~800 |
| `runtime_client.py` | LLM API 适配层，统一 `generate_text()` 接口 | ~360 |
| `run_app.py` | 启动脚本，检查 pip 依赖 + CLI 可用性 + API Key | ~170 |

## 8 阶段流水线

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

阶段间通过文件系统通信（`output/temp_{session_id}/`），每阶段产出文件供下游读取。

**特殊阶段**：
- `description-writer`：分 6 次 LLM 调用分段生成（技术领域/背景技术/发明内容/附图说明/实施例×2），合并后若不足 10000 字自动扩写
- `markdown-merger`：纯文件拼接，无 LLM 调用
- **Fast 模式**：进入流水线前先用 LLM 将简要构思扩写为结构化技术交底书

## 输出目录结构

```
output/temp_{session_id}/
├── 01_input/parsed_info.json          # 必要
├── 02_research/{similar_patents.json, prior_art_analysis.md, writing_style_guide.md}
├── 03_outline/{patent_outline.md, structure_mapping.json}
├── 04_content/{abstract.md, claims.md, description.md}  # 必要
├── 05_diagrams/**/*.mmd
└── 06_final/complete_patent.md        # 必要（最终产物）
```

失败日志：`output/temp_{session_id}/{agent_name}_error.log`

## 代理定义文件

- `.claude/agents/*.md`：8 个子代理（CLI 模式下由 claude CLI 自动加载）
- `patent-writer/references/agents/*.md`：相同内容的副本（Native 模式下由 pipeline_runner.py 读取为 system_prompt）
- `PATENT_SKILL.md` + `patent-writer/references/patent-writing-guide.md`：大型写作指南，在 outline-generator 和 description-writer 阶段截断后注入 prompt

## 环境变量与配置

API Key 配置（至少配一组）：
- `ANTHROPIC_API_KEY`（或 `ANTHROPIC_AUTH_TOKEN`）：Anthropic 后端
- `OPENAI_API_KEY`：OpenAI 后端

运行时选择：
- `PATENT_RUNTIME_MODE`：`native`（默认）或 `cli`
- `PATENT_RUNTIME_BACKEND`：`anthropic`（默认）或 `openai`
- `PATENT_CLI_BACKEND`：`claude`（默认）/ `codex` / `gemini`

模型覆盖：
- `ANTHROPIC_MODEL`（默认 `claude-3-5-sonnet-latest`）、`ANTHROPIC_BASE_URL`
- `OPENAI_MODEL`（默认 `gpt-4o-mini`）、`OPENAI_BASE_URL`、`OPENAI_API_MODE`（`responses`/`chat`）

**配置自动加载**：`runtime_client.py` 在模块加载时读取 `.claude/settings.local.json` 的 `env` 字段并注入 `os.environ`（同名系统环境变量优先）。

## 修改代码时注意

- **双运行时对称**：修改流水线逻辑时，需同时考虑 CLI 和 Native 两条路径
- **代理定义双份**：`.claude/agents/*.md` 和 `patent-writer/references/agents/*.md` 内容需保持同步
- **进程管理**：Web UI 通过 `psutil` + `os.killpg` 管理子进程树，`*.pid.json` 文件跨 Streamlit 重启保持会话恢复
- **文件系统是唯一的阶段间通信方式**：不要引入数据库或消息队列
- **所有注释和文档使用中文**
