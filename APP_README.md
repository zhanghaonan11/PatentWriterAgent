# 专利提案生成器 - Streamlit Web应用

这是一个基于 Streamlit 的 Web 应用，支持单分支双运行时：
- `CLI runtime`：调用 `claude/codex/gemini` CLI
- `Native runtime`：直接调用 Python SDK（Anthropic/OpenAI）

输入模式：
- Normal：上传 DOCX 技术交底书
- Fast：输入简要发明构思，自动扩写后再生成

## 快速开始

```bash
pip install -r requirements.txt
python run_app.py
```

## 侧边栏关键选项

- `Execution mode`：切换 `CLI runtime` / `Native runtime`
- `CLI backend`：`claude` / `codex` / `gemini`
- `Native backend`：`anthropic` / `openai`
- `Input mode`：`normal` / `fast`

## 依赖要求

- CLI runtime：安装对应 CLI 可执行文件
- Native runtime：配置对应 API Key
  - `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`
  - 或 `OPENAI_API_KEY`

## 输出目录

```
output/
├── {session_id}.log
├── {session_id}.pid.json
└── temp_{session_id}/
    ├── 01_input/
    ├── 02_research/
    ├── 03_outline/
    ├── 04_content/
    ├── 05_diagrams/
    └── 06_final/
```
