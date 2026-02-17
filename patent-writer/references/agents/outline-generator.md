# 阶段 3：大纲生成（outline-generator）

根据技术方案和相似专利分析设计专利大纲。执行前必须先读取 `references/patent-writing-guide.md`。

## 执行步骤

1. 读取 `01_input/parsed_info.json` 和 `02_research/similar_patents.json`（如有）
2. 设计完整的专利大纲，包括：
   - 说明书摘要（<300字）
   - 权利要求书（独权+从权，多方面保护）
   - 说明书：技术领域、背景技术、发明内容、附图说明、具体实施方式（>10000字）
3. 为每个章节设置：章节 ID、标题、字数要求、写作要点
4. 保存到 `03_outline/` 目录：
   - `patent_outline.md`：大纲文档
   - `structure_mapping.json`：结构映射

## 输出格式参考

```json
{
  "patent_title": "一种XXX的方法和装置",
  "sections": [
    {
      "id": "01_abstract",
      "title": "说明书摘要",
      "max_words": 300,
      "requirements": ["包含技术问题、技术方案、有益效果"]
    }
  ]
}
```

## 要求

- 大纲必须符合中国专利法规定的标准格式
- 具体实施方式章节必须标注 min_words: 10000
- 权利要求必须包含方法、装置、设备、介质多方面保护
