# CLAUDE.md

This file provides guidance when working with code in this repository.

## 项目概述

专利写作多智能体系统（PatentWriterAgent）：基于子代理（subagent）架构，将技术交底书（.docx）自动转换为符合中国《专利法》规范的完整专利申请文件。

## 开发命令

```bash
# 环境安装
pip install -r requirements.txt

# 配置（至少配置一个模型后端）
# Anthropic-compatible
export ANTHROPIC_API_KEY=xxxx
# 或使用兼容网关：ANTHROPIC_BASE_URL / ANTHROPIC_MODEL

# OpenAI-compatible
# export OPENAI_API_KEY=xxxx
# 可选：OPENAI_BASE_URL / OPENAI_MODEL

# Web 应用（Streamlit）
python run_app.py                       # 自动检查依赖并启动
streamlit run patent_writer_app.py      # 直接启动，访问 http://localhost:8501
python run_app.py --check-only          # 仅检查依赖与后端可用性

# 命令行流水线（无 CLI）
python pipeline_runner.py \
  --session-id 11111111-2222-3333-4444-555555555555 \
  --input-path data/输入.docx \
  --runtime-backend anthropic

# Docker
docker build -t patent-writer .
docker run -p 8009:8009 patent-writer   # Streamlit 端口为 8009
```

## 架构设计

### 核心模式：Python Runner + 8 个专业子代理的流水线编排

核心逻辑在以下文件：
- `pipeline_runner.py`：8 阶段执行、重试、产物校验
- `runtime_client.py`：模型后端适配（Anthropic/OpenAI）
- `.claude/agents/*.md`：各阶段职责指令

### 子代理流水线（严格顺序执行）

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

| 子代理 | 输入 | 输出 | 关键约束 |
|--------|------|------|----------|
| input-parser | raw_document.docx | 01_input/parsed_info.json | 使用 markitdown 转换 docx |
| patent-searcher | parsed_info.json | 02_research/similar_patents.json, prior_art_analysis.md | 无 MCP 时可降级 |
| outline-generator | parsed_info.json + similar_patents.json | 03_outline/patent_outline.md, structure_mapping.json | 必须读取 PATENT_SKILL.md |
| abstract-writer | patent_outline.md | 04_content/abstract.md | ≤300 字 |
| claims-writer | patent_outline.md + abstract.md | 04_content/claims.md | 方法+装置+设备+介质四类独权 |
| description-writer | patent_outline.md + claims.md | 04_content/description.md | >10000 字，≤3 个实施例 |
| diagram-generator | description.md + structure_mapping.json | 05_diagrams/**/*.mmd | Mermaid 格式 |
| markdown-merger | 04_content/* + 05_diagrams/* | 06_final/complete_patent.md | 术语一致性校验 |

### 工作目录结构

每次执行创建 `output/temp_[uuid]/`，包含 6 个阶段子目录（01_input → 06_final）加 metadata/。

### MCP 工具依赖（可选）

- `@kunihiros/google-patents-mcp`：专利检索
- `exa-mcp-server`：Web 搜索/技术文献

## 专利写作编排指令

1. 使用用户提供的 UUID 创建 `output/temp_[uuid]/` 工作目录
2. 按流水线顺序依次执行并验证输出完整性
3. 阶段失败写入 `output/temp_[uuid]/[agent_name]_error.log`，最多重试 3 次
4. 最终交付：`output/temp_[uuid]/06_final/complete_patent.md`

### 质量标准

- 严格遵循中国《专利法》和《专利审查指南》规范
- 具体实施方式 > 10000 字，实施例 ≤ 3 个
- 全文术语一致，章节逻辑链条完整
- JSON 使用 2 空格缩进，Mermaid 图表扩展名 `.mmd`
- 图表引用与实际文件名匹配

### 子代理目录映射

| 阶段 | 目录 | 文件 |
|------|------|------|
| 输入解析 | 01_input/ | raw_document.docx, parsed_info.json |
| 专利研究 | 02_research/ | similar_patents.json, prior_art_analysis.md, writing_style_guide.md |
| 大纲生成 | 03_outline/ | patent_outline.md, structure_mapping.json |
| 内容撰写 | 04_content/ | abstract.md, claims.md, description.md, figures.md |
| 图表生成 | 05_diagrams/ | flowcharts/*.mmd, structural_diagrams/*.mmd, sequence_diagrams/*.mmd |
| 最终输出 | 06_final/ | complete_patent.md, summary_report.md |
