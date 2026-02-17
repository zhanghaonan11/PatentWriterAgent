# 阶段 2：专利检索（patent-searcher）

使用 MCP 工具搜索相似专利和技术文献，学习写作风格。

## 执行步骤

1. 根据 `parsed_info.json` 中的技术关键词，使用 MCP 工具搜索相似专利
2. 使用 `mcp__google-patents-mcp__search_patents` 搜索 Google Patents
   - 优先搜索中国专利（CHINESE）
   - 搜索 GRANT 状态的授权专利
   - 返回前 10 个最相关结果
3. 使用 `mcp__exa__web_search_exa` 搜索技术文档和论文
4. 分析搜索结果，识别最相关的 5-10 个专利
5. 保存结果到 `02_research/` 目录：
   - `similar_patents.json`：结构化专利摘要
   - `prior_art_analysis.md`：现有技术分析报告
   - `writing_style_guide.md`：写作风格参考

## 要求

- 检索的专利仅用于学习写作风格和技术描述方式
- **严禁抄袭任何专利内容**
- 重点关注：技术术语使用、章节结构、描述方式
- MCP 工具不可用时跳过此阶段
