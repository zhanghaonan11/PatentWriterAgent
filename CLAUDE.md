# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

专利写作多智能体系统（PatentWriterAgent）：基于 Claude Code 的子代理（subagent）架构，将技术交底书（.docx）自动转换为符合中国《专利法》规范的完整专利申请文件。

## 开发命令

```bash
# 环境安装
npm install -g @anthropic-ai/claude-code
pip install -r requirements.txt

# 配置（必须完成才能运行）
cp .mcp.json.example .mcp.json          # 填入 SERPAPI_API_KEY 和 EXA_API_KEY
cp .claude/settings.local.json.example .claude/settings.local.json  # 配置模型 Token/URL

# CLI 执行专利生成
claude --dangerously-skip-permissions "根据 data/输入.docx 编写专利提案" -p --output-format stream-json --verbose

# Web 应用（Streamlit）
python run_app.py                       # 自动检查依赖并启动
streamlit run patent_writer_app.py      # 直接启动，访问 http://localhost:8501

# Docker
docker build -t patent-writer .
docker run -p 8009:8009 patent-writer   # Streamlit 端口为 8009
```

## 架构设计

### 核心模式：主代理 + 8 个专业子代理的流水线编排

本仓库没有传统的应用代码。核心逻辑全部在 **CLAUDE.md**（主代理指令）和 `.claude/agents/*.md`（子代理定义）中，由 Claude Code 运行时解释执行。

### 子代理流水线（严格顺序执行）

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

每个子代理定义在 `.claude/agents/{name}.md`，职责如下：

| 子代理 | 输入 | 输出 | 关键约束 |
|--------|------|------|----------|
| input-parser | raw_document.docx | 01_input/parsed_info.json | 使用 markitdown 转换 docx |
| patent-searcher | parsed_info.json | 02_research/similar_patents.json, prior_art_analysis.md | 调用 Google Patents MCP + Exa MCP |
| outline-generator | parsed_info.json + similar_patents.json | 03_outline/patent_outline.md, structure_mapping.json | 必须读取 PATENT_SKILL.md |
| abstract-writer | patent_outline.md | 04_content/abstract.md | ≤300 字 |
| claims-writer | patent_outline.md + abstract.md | 04_content/claims.md | 方法+装置+设备+介质四类独权 |
| description-writer | patent_outline.md + claims.md | 04_content/description.md | **>10000 字**，≤3 个实施例 |
| diagram-generator | description.md + structure_mapping.json | 05_diagrams/**/*.mmd | Mermaid 格式 |
| markdown-merger | 04_content/* + 05_diagrams/* | 06_final/complete_patent.md | 术语一致性校验 |

### 工作目录结构

每次执行创建 `output/temp_[uuid]/`，包含 6 个阶段子目录（01_input → 06_final）加 metadata/。子代理通过文件系统传递数据，JSON 文件作为结构化接口。

### MCP 工具依赖

- **google-patents-mcp**（`@kunihiros/google-patents-mcp`）：专利检索，需 SERPAPI_API_KEY
- **exa**（`exa-mcp-server`）：Web 搜索/技术文献，需 EXA_API_KEY

### 关键文件

- `PATENT_SKILL.md`：专利撰写规范指南（所有写作类子代理必须先读取此文件）
- `data/example_patent.md`：示例输出参考
- `output/temp_9ba0a678-*/`：已有的示例输出目录

## 专利写作编排指令

当收到专利写作请求时，作为主代理需要：

1. 使用用户提供的 UUID 创建 `output/temp_[uuid]/` 工作目录（无 UUID 则使用时间戳）
2. 按上述流水线顺序依次调用子代理，每步完成后验证输出完整性
3. 子代理失败时记录错误日志到 `output/temp_[uuid]/[agent_name]_error.log`，最多重试 3 次
4. 最终交付路径：`output/temp_[uuid]/06_final/complete_patent.md`

### 质量标准

- 严格遵循中国《专利法》和《专利审查指南》规范
- 具体实施方式 > 10000 字，实施例 ≤ 3 个
- 全文术语一致，章节逻辑链条完整
- JSON 使用 2 空格缩进，Mermaid 图表以 `.mmd` 为扩展名
- 图表引用必须与实际文件名匹配

### 子代理目录映射

| 阶段 | 目录 | 文件 |
|------|------|------|
| 输入解析 | 01_input/ | raw_document.docx, parsed_info.json |
| 专利研究 | 02_research/ | similar_patents.json, prior_art_analysis.md, writing_style_guide.md |
| 大纲生成 | 03_outline/ | patent_outline.md, structure_mapping.json |
| 内容撰写 | 04_content/ | abstract.md, claims.md, description.md, figures.md |
| 图表生成 | 05_diagrams/ | flowcharts/*.mmd, structural_diagrams/*.mmd, sequence_diagrams/*.mmd |
| 最终输出 | 06_final/ | complete_patent.md, summary_report.md |
