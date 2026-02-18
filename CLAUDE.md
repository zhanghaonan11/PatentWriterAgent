# CLAUDE.md

This file provides guidance when working with code in this repository.

## 项目概述

PatentWriterAgent 采用单分支双运行时架构：
- CLI runtime：兼容 `claude/codex/gemini`
- Native runtime：通过 Python SDK 调 Anthropic/OpenAI

## 开发命令

```bash
pip install -r requirements.txt
python run_app.py --check-only
python run_app.py
```

## 运行时结构

- `patent_writer_app.py`：统一 Web UI，按 execution mode 路由
- `run_app.py`：检查依赖、CLI 可用性、native 后端可用性
- `pipeline_runner.py`：native 模式下 8 阶段执行器
- `runtime_client.py`：native 后端适配

## 流水线顺序

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

## 输出要求

- 必要输出：
  - `01_input/parsed_info.json`
  - `04_content/{abstract,claims,description}.md`
  - `06_final/complete_patent.md`
- 失败日志：`output/temp_[uuid]/[agent_name]_error.log`
