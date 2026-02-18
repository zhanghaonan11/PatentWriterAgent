# CLAUDE.md

This file provides guidance when working with code in this repository.

## 项目概述

专利写作多智能体系统（PatentWriterAgent）：基于子代理（subagent）架构，将技术交底书（.docx）自动转换为符合中国《专利法》规范的完整专利申请文件。

## 开发命令

```bash
# 环境安装
pip install -r requirements.txt

# 至少配置一个模型后端
export ANTHROPIC_API_KEY=xxxx
# 或
# export OPENAI_API_KEY=xxxx

# Web 应用（Streamlit）
python run_app.py
streamlit run patent_writer_app.py
python run_app.py --check-only

# 命令行流水线（无 CLI）
python pipeline_runner.py \
  --session-id 11111111-2222-3333-4444-555555555555 \
  --input-path data/输入.docx \
  --runtime-backend anthropic

# Docker
docker build -t patent-writer .
docker run -p 8009:8009 patent-writer
```

## 架构设计

核心执行逻辑：
- `pipeline_runner.py`：8 阶段执行、重试、产物校验
- `runtime_client.py`：模型后端适配（Anthropic/OpenAI）
- `.claude/agents/*.md`：各阶段职责指令

流水线顺序：

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

## 输出路径规范

每次执行创建：`output/temp_[uuid]/`

关键产物：
- `01_input/parsed_info.json`
- `04_content/{abstract,claims,description}.md`
- `06_final/complete_patent.md`

错误日志：`output/temp_[uuid]/[agent_name]_error.log`（最多重试 3 次）
