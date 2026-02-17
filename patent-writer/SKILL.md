---
name: patent-writer
description: "Chinese patent application document generator. Converts technical disclosure documents (.docx) into complete, legally compliant patent applications following Chinese Patent Law and Patent Examination Guidelines. Use when user asks to: write a patent, generate patent application, draft patent claims, create patent document, convert technical disclosure to patent, write 专利申请, 撰写专利, 生成专利文件, or any patent writing task targeting Chinese patents. Supports full pipeline: document parsing, prior art search, outline generation, abstract writing, claims drafting, detailed description (over 10000 words), diagram generation (Mermaid), and final document assembly."
---

# Patent Writer - 专利写作智能体

将技术交底书自动转换为符合中国《专利法》规范的完整专利申请文件。

## 前置依赖

**MCP 工具（可选，用于专利检索）：**
- `@kunihiros/google-patents-mcp`：需要 SERPAPI_API_KEY
- `exa-mcp-server`：需要 EXA_API_KEY

**系统工具：**
- `markitdown`：`pip install markitdown[docx]`（用于 docx 转 markdown）

若 MCP 工具不可用，跳过专利检索阶段，直接基于交底书内容生成。

## 工作流程

收到专利写作请求后，严格按以下 8 阶段流水线顺序执行：

```
input-parser → patent-searcher → outline-generator → abstract-writer
→ claims-writer → description-writer → diagram-generator → markdown-merger
```

### 初始化

1. 创建工作目录 `output/temp_[uuid]/`（无 UUID 则用时间戳）
2. 创建 6 个阶段子目录：`01_input/` `02_research/` `03_outline/` `04_content/` `05_diagrams/` `06_final/`

### 阶段 1：文档解析（input-parser）

读取 [references/agents/input-parser.md](references/agents/input-parser.md) 获取完整指令。

- 输入：用户提供的 .docx 文件
- 使用 `markitdown <input.docx> -o output.md` 转换
- 输出：`01_input/parsed_info.json`（结构化 JSON）

### 阶段 2：专利检索（patent-searcher）

读取 [references/agents/patent-searcher.md](references/agents/patent-searcher.md) 获取完整指令。

- 输入：`parsed_info.json`
- 调用 MCP 工具搜索相似专利和技术文献
- 输出：`02_research/similar_patents.json`、`prior_art_analysis.md`、`writing_style_guide.md`
- **注意**：MCP 不可用时跳过此阶段

### 阶段 3：大纲生成（outline-generator）

读取 [references/agents/outline-generator.md](references/agents/outline-generator.md) 和 [references/patent-writing-guide.md](references/patent-writing-guide.md) 获取完整指令。

- 输入：`parsed_info.json` + `similar_patents.json`
- 输出：`03_outline/patent_outline.md`、`structure_mapping.json`
- 关键：具体实施方式标注 min_words: 10000

### 阶段 4：摘要撰写（abstract-writer）

读取 [references/agents/abstract-writer.md](references/agents/abstract-writer.md) 和 [references/patent-writing-guide.md](references/patent-writing-guide.md)。

- 输入：`patent_outline.md`
- 输出：`04_content/abstract.md`（≤300 字）

### 阶段 5：权利要求书（claims-writer）

读取 [references/agents/claims-writer.md](references/agents/claims-writer.md) 和 [references/patent-writing-guide.md](references/patent-writing-guide.md)。

- 输入：`patent_outline.md` + `abstract.md`
- 输出：`04_content/claims.md`
- 结构：方法独权 + 从属(5-10项) + 装置独权 + 从属(3-5项) + 电子设备独权 + 存储介质独权

### 阶段 6：说明书撰写（description-writer）

读取 [references/agents/description-writer.md](references/agents/description-writer.md) 和 [references/patent-writing-guide.md](references/patent-writing-guide.md)。

- 输入：`patent_outline.md` + `claims.md`
- 输出：`04_content/description.md`
- **核心要求**：具体实施方式 >10000 字，实施例 ≤3 个

### 阶段 7：图表生成（diagram-generator）

读取 [references/agents/diagram-generator.md](references/agents/diagram-generator.md)。

- 输入：`description.md` + `structure_mapping.json`
- 输出：`05_diagrams/flowcharts/*.mmd`、`structural_diagrams/*.mmd`、`sequence_diagrams/*.mmd`

### 阶段 8：文档合并（markdown-merger）

读取 [references/agents/markdown-merger.md](references/agents/markdown-merger.md) 和 [references/patent-writing-guide.md](references/patent-writing-guide.md)。

- 输入：`04_content/*` + `05_diagrams/*`
- 输出：`06_final/complete_patent.md`
- 质量检查：章节完整性、术语一致性、字数达标、图表编号匹配

## 质量标准

- 严格遵循中国《专利法》和《专利审查指南》规范
- 具体实施方式 >10000 字，实施例 ≤3 个
- 全文术语一致，章节逻辑链条完整
- Mermaid 图表以 `.mmd` 为扩展名
- 图表引用必须与实际文件名匹配
- 子代理失败时记录错误日志，最多重试 3 次

## 错误处理

- 阶段失败：记录到 `output/temp_[uuid]/[agent_name]_error.log`，最多重试 3 次
- MCP 不可用：跳过专利检索阶段，其余阶段正常执行
- docx 解析失败：提示用户检查文件格式，尝试直接读取文本内容

## 示例输出

参考 [assets/example_patent.md](assets/example_patent.md) 查看完整的专利申请文件示例。

## Resources

### references/

- **patent-writing-guide.md**：中国专利申请文件撰写完整规范（832 行），涵盖发明名称、技术领域、背景技术、发明内容、权利要求书、说明书摘要等全部章节的撰写要求
- **agents/**：8 个流水线阶段的详细执行指令
  - `input-parser.md`：文档解析指令
  - `patent-searcher.md`：专利检索指令
  - `outline-generator.md`：大纲生成指令
  - `abstract-writer.md`：摘要撰写指令
  - `claims-writer.md`：权利要求书撰写指令
  - `description-writer.md`：说明书撰写指令
  - `diagram-generator.md`：图表生成指令
  - `markdown-merger.md`：文档合并指令

### assets/

- **example_patent.md**：完整的专利申请文件示例（基于混合协议的大规模文件分发方法和装置）
