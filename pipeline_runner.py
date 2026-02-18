#!/usr/bin/env python3
"""Native Python patent pipeline runner without external AI CLI runtimes."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from markitdown import MarkItDown

from runtime_client import (
    DEFAULT_RUNTIME_BACKEND,
    RuntimeClientError,
    generate_text,
    get_runtime_label,
    is_runtime_available,
    runtime_setup_hint,
)


ROOT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT_DIR / "output"
AGENTS_DIR = ROOT_DIR / ".claude" / "agents"
PATENT_SKILL_PATH = ROOT_DIR / "PATENT_SKILL.md"
PATENT_GUIDE_PATH = ROOT_DIR / "patent-writer" / "references" / "patent-writing-guide.md"


def log(message: str) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def trim_context(text: str, limit: int = 24000) -> str:
    value = text.strip()
    if len(value) <= limit:
        return value
    head = value[: limit // 2]
    tail = value[-(limit // 2) :]
    return head + "\n\n...[truncated for context size]...\n\n" + tail


def normalize_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = re.split(r"[\n,，;；]", value)
        return [part.strip() for part in parts if part.strip()]
    return []


def extract_json_from_text(text: str) -> Optional[Any]:
    raw = text.strip()
    if not raw:
        return None

    for candidate in (raw,):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    for block in fenced:
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    match = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.DOTALL)
    if match:
        snippet = match.group(1).strip()
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return None
    return None


def extract_block(text: str, start_tag: str, end_tag: str) -> str:
    pattern = re.compile(
        re.escape(start_tag) + r"(.*?)" + re.escape(end_tag),
        flags=re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip()


def extract_first_mermaid_block(text: str) -> str:
    match = re.search(r"```mermaid\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def load_agent_instruction(agent_name: str) -> str:
    return read_text(AGENTS_DIR / f"{agent_name}.md")


def normalize_parsed_info(payload: Dict[str, Any]) -> Dict[str, Any]:
    title = str(payload.get("title", "")).strip() or "一种数据处理方法、装置、设备及存储介质"
    technical_problem = str(payload.get("technical_problem", "")).strip() or "提升处理效率并降低资源消耗。"
    technical_solution = str(payload.get("technical_solution", "")).strip() or "通过模块化流程和参数化策略实现目标任务处理。"

    existing_solutions = normalize_list(payload.get("existing_solutions"))
    if not existing_solutions:
        existing_solutions = ["基于单一规则引擎的处理方案", "基于集中式调度的处理方案"]

    existing_drawbacks = normalize_list(payload.get("existing_drawbacks"))
    if not existing_drawbacks:
        existing_drawbacks = ["扩展性不足", "异常场景处理能力弱"]

    benefits = normalize_list(payload.get("benefits"))
    if not benefits:
        benefits = ["提高处理吞吐能力", "降低系统资源占用", "增强异常处理稳定性"]

    keywords = normalize_list(payload.get("keywords"))
    if not keywords:
        keywords = ["数据处理", "调度", "异常恢复", "并发", "参数优化"]

    return {
        "title": title,
        "technical_problem": technical_problem,
        "existing_solutions": existing_solutions,
        "existing_drawbacks": existing_drawbacks,
        "technical_solution": technical_solution,
        "benefits": benefits,
        "keywords": keywords,
    }


def llm_generate(
    runtime_backend: str,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout_seconds: int = 900,
) -> str:
    return generate_text(
        runtime_backend=runtime_backend,
        prompt=prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )


def stage_input_parser(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]
    input_path: Path = ctx["input_path"]

    stage_dir = session_dir / "01_input"
    stage_dir.mkdir(parents=True, exist_ok=True)

    raw_doc_path = stage_dir / "raw_document.docx"
    shutil.copy2(input_path, raw_doc_path)

    converter = MarkItDown()
    conversion = converter.convert(str(raw_doc_path))
    markdown = (conversion.text_content or "").strip()
    if not markdown:
        raise RuntimeError("Failed to extract text from input document")

    raw_markdown_path = stage_dir / "raw_document.md"
    write_text(raw_markdown_path, markdown)

    instruction = trim_context(load_agent_instruction("input-parser"), 8000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 2000)

    prompt = f"""你是专利文档解析器，请严格输出 JSON（不要输出额外说明）。

目标结构：
{{
  "title": "",
  "technical_problem": "",
  "existing_solutions": [""],
  "existing_drawbacks": [""],
  "technical_solution": "",
  "benefits": [""],
  "keywords": [""]
}}

提取要求：
1. 仅根据文档内容抽取，不要编造事实；缺失可做最小合理补全。
2. existing_solutions / existing_drawbacks / benefits / keywords 必须是数组。
3. keywords 提供 5-10 个专业关键词。

附加任务要求（如有）：
{task_prompt or "无"}

参考执行指令：
{instruction}

文档内容：
{trim_context(markdown, 30000)}
"""

    response = llm_generate(
        runtime_backend,
        prompt,
        max_tokens=2400,
        temperature=0.1,
    )
    parsed = extract_json_from_text(response)
    if not isinstance(parsed, dict):
        raise RuntimeError("input-parser did not return valid JSON")

    normalized = normalize_parsed_info(parsed)
    write_json(stage_dir / "parsed_info.json", normalized)


def stage_patent_searcher(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]
    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})

    stage_dir = session_dir / "02_research"
    stage_dir.mkdir(parents=True, exist_ok=True)

    instruction = trim_context(load_agent_instruction("patent-searcher"), 8000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 2000)

    prompt = f"""你是专利检索分析助手。在无法直接调用外部检索工具时，请基于技术主题给出“建议检索方向 + 可参考专利类型”的分析结果。

请严格按以下格式输出，且不要输出其他内容：
<<<SIMILAR_PATENTS_JSON>>>
[{{"title":"","publication_no":"","country":"CN","relevance":0.9,"key_points":[""],"analysis":""}}]
<<<END_SIMILAR_PATENTS_JSON>>>
<<<PRIOR_ART_ANALYSIS_MD>>>
# 现有技术分析
...
<<<END_PRIOR_ART_ANALYSIS_MD>>>
<<<WRITING_STYLE_GUIDE_MD>>>
# 写作风格建议
...
<<<END_WRITING_STYLE_GUIDE_MD>>>

约束：
1. similar patents 输出 5-10 条。
2. 严禁抄袭现有专利原文，重点给“结构和写作风格参考”。
3. 结论需要支持后续权利要求、说明书撰写。

附加任务要求（如有）：
{task_prompt or "无"}

参考执行指令：
{instruction}

输入（parsed_info）：
{json.dumps(parsed_info, ensure_ascii=False, indent=2)}
"""

    response = llm_generate(runtime_backend, prompt, max_tokens=3800, temperature=0.2)

    similar_raw = extract_block(response, "<<<SIMILAR_PATENTS_JSON>>>", "<<<END_SIMILAR_PATENTS_JSON>>>")
    analysis_md = extract_block(response, "<<<PRIOR_ART_ANALYSIS_MD>>>", "<<<END_PRIOR_ART_ANALYSIS_MD>>>")
    style_md = extract_block(response, "<<<WRITING_STYLE_GUIDE_MD>>>", "<<<END_WRITING_STYLE_GUIDE_MD>>>")

    similar_data = extract_json_from_text(similar_raw)
    if not isinstance(similar_data, list):
        similar_data = []

    normalized_similar: List[Dict[str, Any]] = []
    for item in similar_data[:10]:
        if not isinstance(item, dict):
            continue
        normalized_similar.append(
            {
                "title": str(item.get("title", "未命名参考专利")).strip() or "未命名参考专利",
                "publication_no": str(item.get("publication_no", "N/A")).strip() or "N/A",
                "country": str(item.get("country", "CN")).strip() or "CN",
                "relevance": float(item.get("relevance", 0.6)) if str(item.get("relevance", "")).strip() else 0.6,
                "key_points": normalize_list(item.get("key_points"))[:6],
                "analysis": str(item.get("analysis", "")).strip(),
            }
        )

    if not normalized_similar:
        keywords = normalize_list(parsed_info.get("keywords"))
        normalized_similar = [
            {
                "title": f"面向{keyword}的改进型技术方案",
                "publication_no": f"CN-REF-{idx + 1:03d}",
                "country": "CN",
                "relevance": 0.65,
                "key_points": ["流程模块化", "可扩展处理", "稳定性增强"],
                "analysis": "可用于学习背景技术与有益效果写法。",
            }
            for idx, keyword in enumerate(keywords[:5] or ["数据处理", "系统架构", "异常恢复", "并发控制", "资源调度"])
        ]

    if not analysis_md.strip():
        analysis_md = "# 现有技术分析\n\n未获取到外部检索结果，已基于输入技术主题生成检索方向与对比思路。"

    if not style_md.strip():
        style_md = "# 写作风格建议\n\n- 使用客观、法律化语句\n- 强化步骤编号、模块编号\n- 保持术语前后一致"

    write_json(stage_dir / "similar_patents.json", normalized_similar)
    write_text(stage_dir / "prior_art_analysis.md", analysis_md.strip() + "\n")
    write_text(stage_dir / "writing_style_guide.md", style_md.strip() + "\n")


def stage_outline_generator(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]

    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})
    similar_patents = read_json(session_dir / "02_research" / "similar_patents.json", [])
    patent_guide = trim_context(read_text(PATENT_GUIDE_PATH), 16000)
    skill_guide = trim_context(read_text(PATENT_SKILL_PATH), 12000)
    instruction = trim_context(load_agent_instruction("outline-generator"), 8000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 2000)

    prompt = f"""你是专利大纲设计专家。请输出两个区块，不要输出其他内容：

<<<PATENT_OUTLINE_MD>>>
# 专利大纲
...
<<<END_PATENT_OUTLINE_MD>>>

<<<STRUCTURE_MAPPING_JSON>>>
{{
  "patent_title": "",
  "sections": [
    {{"id":"01_abstract","title":"说明书摘要","min_words":200,"max_words":300,"requirements":[""]}}
  ]
}}
<<<END_STRUCTURE_MAPPING_JSON>>>

要求：
1. 结构必须覆盖摘要、权利要求书、说明书全章节。
2. 明确“具体实施方式”min_words >= 10000。
3. 权利要求保护维度覆盖方法、装置/系统、设备、存储介质。
4. section id 使用稳定英文下划线命名。

附加任务要求（如有）：
{task_prompt or "无"}

参考执行指令：
{instruction}

专利写作指南（节选）：
{patent_guide}

专利技能规范（节选）：
{skill_guide}

输入 parsed_info：
{json.dumps(parsed_info, ensure_ascii=False, indent=2)}

输入 similar_patents（摘要）：
{json.dumps(similar_patents[:6], ensure_ascii=False, indent=2)}
"""

    response = llm_generate(runtime_backend, prompt, max_tokens=5000, temperature=0.2)

    outline_md = extract_block(response, "<<<PATENT_OUTLINE_MD>>>", "<<<END_PATENT_OUTLINE_MD>>>")
    structure_raw = extract_block(
        response,
        "<<<STRUCTURE_MAPPING_JSON>>>",
        "<<<END_STRUCTURE_MAPPING_JSON>>>",
    )

    structure_json = extract_json_from_text(structure_raw)
    if not isinstance(structure_json, dict):
        raise RuntimeError("outline-generator did not return valid structure_mapping JSON")

    stage_dir = session_dir / "03_outline"
    stage_dir.mkdir(parents=True, exist_ok=True)

    if not outline_md.strip():
        outline_md = f"# 专利大纲\n\n- 专利名称：{parsed_info.get('title', '待定')}\n- 包含摘要、权利要求书、说明书及附图。"

    write_text(stage_dir / "patent_outline.md", outline_md.strip() + "\n")
    write_json(stage_dir / "structure_mapping.json", structure_json)


def stage_abstract_writer(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]

    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})
    outline_md = read_text(session_dir / "03_outline" / "patent_outline.md")
    guide = trim_context(read_text(PATENT_GUIDE_PATH), 12000)
    skill_guide = trim_context(read_text(PATENT_SKILL_PATH), 10000)
    instruction = trim_context(load_agent_instruction("abstract-writer"), 6000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 1500)

    prompt = f"""请撰写中国专利说明书摘要，要求：
1. 必须以“本申请公开了”开头。
2. 不超过300字。
3. 包含技术问题、技术方案、有益效果。
4. 法律语言准确，无宣传措辞。
5. 仅输出摘要正文。

附加任务要求（如有）：
{task_prompt or "无"}

参考执行指令：
{instruction}

写作指南（节选）：
{guide}

专利技能规范（节选）：
{skill_guide}

输入信息：
parsed_info={json.dumps(parsed_info, ensure_ascii=False, indent=2)}

大纲：
{trim_context(outline_md, 12000)}
"""

    response = llm_generate(runtime_backend, prompt, max_tokens=900, temperature=0.1)
    abstract = response.strip()
    if not abstract.startswith("本申请公开了"):
        abstract = "本申请公开了" + abstract.lstrip("，,。 .")
    if len(abstract) > 320:
        abstract = abstract[:320].rstrip() + "。"

    write_text(session_dir / "04_content" / "abstract.md", abstract + "\n")


def stage_claims_writer(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]

    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})
    outline_md = read_text(session_dir / "03_outline" / "patent_outline.md")
    abstract_md = read_text(session_dir / "04_content" / "abstract.md")
    guide = trim_context(read_text(PATENT_GUIDE_PATH), 12000)
    skill_guide = trim_context(read_text(PATENT_SKILL_PATH), 10000)
    instruction = trim_context(load_agent_instruction("claims-writer"), 7000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 1500)

    prompt = f"""请生成专利权利要求书 Markdown，要求：
1. 至少包含：方法独立权利要求1项+方法从属5-10项+装置/系统独立1项+装置从属3-5项+电子设备独立1项+存储介质独立1项。
2. 使用规范句式：
   - "1. 一种...方法，其特征在于，包括："
   - "2. 根据权利要求1所述的方法，其特征在于，..."
3. 方法步骤使用分号（；）分隔。
4. 术语与摘要、大纲一致。
5. 仅输出最终 Markdown 内容。

附加任务要求（如有）：
{task_prompt or "无"}

参考执行指令：
{instruction}

写作指南（节选）：
{guide}

专利技能规范（节选）：
{skill_guide}

输入 parsed_info：
{json.dumps(parsed_info, ensure_ascii=False, indent=2)}

摘要：
{abstract_md}

大纲：
{trim_context(outline_md, 12000)}
"""

    claims_md = llm_generate(runtime_backend, prompt, max_tokens=4200, temperature=0.2).strip()
    if not re.search(r"^\s*1\.", claims_md, flags=re.MULTILINE):
        claims_md = "1. 一种数据处理方法，其特征在于，包括：\n获取输入数据；\n执行目标处理流程；\n输出处理结果。\n\n" + claims_md

    write_text(session_dir / "04_content" / "claims.md", claims_md + "\n")


def generate_long_section(
    runtime_backend: str,
    heading: str,
    min_chars: int,
    context_prompt: str,
    *,
    max_tokens: int = 3200,
) -> str:
    prompt = f"""请仅输出“{heading}”章节正文，不要输出其他标题。
要求：
1. 中文技术写作风格，法律化、客观、可实施。
2. 术语与上下文保持一致。
3. 最少 {min_chars} 个中文字符。

上下文：
{context_prompt}
"""

    text = llm_generate(
        runtime_backend,
        prompt,
        max_tokens=max_tokens,
        temperature=0.25,
        timeout_seconds=1200,
    ).strip()

    compressed_len = len(re.sub(r"\s+", "", text))
    if compressed_len < min_chars:
        expand_prompt = f"""请在不改变原有技术逻辑的前提下，扩写以下内容并补齐到至少 {min_chars} 个中文字符。
只输出扩写后的完整正文：
{text}
"""
        text = llm_generate(
            runtime_backend,
            expand_prompt,
            max_tokens=max_tokens,
            temperature=0.3,
            timeout_seconds=1200,
        ).strip()
    return text


def stage_description_writer(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]

    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})
    outline_md = read_text(session_dir / "03_outline" / "patent_outline.md")
    abstract_md = read_text(session_dir / "04_content" / "abstract.md")
    claims_md = read_text(session_dir / "04_content" / "claims.md")
    prior_art_md = read_text(session_dir / "02_research" / "prior_art_analysis.md")
    guide = trim_context(read_text(PATENT_GUIDE_PATH), 18000)
    skill_guide = trim_context(read_text(PATENT_SKILL_PATH), 14000)
    instruction = trim_context(load_agent_instruction("description-writer"), 9000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 2000)

    common_context = trim_context(
        "\n\n".join(
            [
                f"parsed_info:\n{json.dumps(parsed_info, ensure_ascii=False, indent=2)}",
                f"outline:\n{outline_md}",
                f"abstract:\n{abstract_md}",
                f"claims:\n{claims_md}",
                f"prior_art:\n{prior_art_md}",
                f"task_prompt:\n{task_prompt or '无'}",
                f"instruction:\n{instruction}",
                f"guide:\n{guide}",
                f"skill_guide:\n{skill_guide}",
            ]
        ),
        30000,
    )

    tech_field = generate_long_section(
        runtime_backend,
        "技术领域",
        220,
        common_context,
        max_tokens=1200,
    )
    background = generate_long_section(
        runtime_backend,
        "背景技术",
        1600,
        common_context,
        max_tokens=2600,
    )
    invention_content = generate_long_section(
        runtime_backend,
        "发明内容",
        2000,
        common_context,
        max_tokens=3000,
    )
    drawing_desc = generate_long_section(
        runtime_backend,
        "附图说明",
        380,
        common_context,
        max_tokens=1400,
    )
    embodiments_part1 = generate_long_section(
        runtime_backend,
        "具体实施方式（实施例一）",
        3800,
        common_context,
        max_tokens=3600,
    )
    embodiments_part2 = generate_long_section(
        runtime_backend,
        "具体实施方式（实施例二及变体）",
        3800,
        common_context,
        max_tokens=3600,
    )

    description = (
        "## 技术领域\n\n"
        + tech_field.strip()
        + "\n\n## 背景技术\n\n"
        + background.strip()
        + "\n\n## 发明内容\n\n"
        + invention_content.strip()
        + "\n\n## 附图说明\n\n"
        + drawing_desc.strip()
        + "\n\n## 具体实施方式\n\n"
        + embodiments_part1.strip()
        + "\n\n"
        + embodiments_part2.strip()
        + "\n"
    )

    size_no_space = len(re.sub(r"\s+", "", description))
    if size_no_space < 10000:
        expansion_prompt = f"""以下是专利说明书草稿，当前长度不足。请在保持术语一致和逻辑完整的前提下扩写“具体实施方式”部分，输出完整的说明书 Markdown。

{description}
"""
        expanded = llm_generate(
            runtime_backend,
            expansion_prompt,
            max_tokens=3800,
            temperature=0.25,
            timeout_seconds=1200,
        ).strip()
        if expanded:
            description = expanded

    write_text(session_dir / "04_content" / "description.md", description.strip() + "\n")


def stage_diagram_generator(ctx: Dict[str, Any]) -> None:
    runtime_backend = ctx["runtime_backend"]
    session_dir: Path = ctx["session_dir"]

    description_md = read_text(session_dir / "04_content" / "description.md")
    structure_mapping = read_json(session_dir / "03_outline" / "structure_mapping.json", {})
    skill_guide = trim_context(read_text(PATENT_SKILL_PATH), 8000)
    instruction = trim_context(load_agent_instruction("diagram-generator"), 7000)
    task_prompt = trim_context(ctx.get("task_prompt", ""), 1200)

    prompt = f"""请输出三个 Mermaid 图，且仅输出以下结构：
<<<FLOWCHART_MERMAID>>>
```mermaid
...
```
<<<END_FLOWCHART_MERMAID>>>
<<<DEVICE_MERMAID>>>
```mermaid
...
```
<<<END_DEVICE_MERMAID>>>
<<<SYSTEM_MERMAID>>>
```mermaid
...
```
<<<END_SYSTEM_MERMAID>>>

要求：
1. 流程图使用 graph TD，步骤编号使用 S101/S102...。
2. 装置图使用 graph TB，模块编号使用 201/202...。
3. 系统图使用 graph LR，体现端-服务-存储协作。
4. 与说明书术语保持一致。

附加任务要求（如有）：
{task_prompt or "无"}

参考执行指令：
{instruction}

专利技能规范（节选）：
{skill_guide}

输入 structure_mapping：
{json.dumps(structure_mapping, ensure_ascii=False, indent=2)}

输入 description（节选）：
{trim_context(description_md, 18000)}
"""

    response = llm_generate(runtime_backend, prompt, max_tokens=2600, temperature=0.2)

    flow_block = extract_block(response, "<<<FLOWCHART_MERMAID>>>", "<<<END_FLOWCHART_MERMAID>>>")
    device_block = extract_block(response, "<<<DEVICE_MERMAID>>>", "<<<END_DEVICE_MERMAID>>>")
    system_block = extract_block(response, "<<<SYSTEM_MERMAID>>>", "<<<END_SYSTEM_MERMAID>>>")

    flow_mmd = extract_first_mermaid_block(flow_block) or (
        "graph TD\n"
        "    A[S101: 获取待处理数据] --> B[S102: 执行特征分析]\n"
        "    B --> C[S103: 进行策略决策]\n"
        "    C --> D[S104: 输出处理结果]"
    )
    device_mmd = extract_first_mermaid_block(device_block) or (
        "graph TB\n"
        "    subgraph 数据处理装置 200\n"
        "        M201[获取模块 201]\n"
        "        M202[分析模块 202]\n"
        "        M203[决策模块 203]\n"
        "        M204[输出模块 204]\n"
        "    end\n"
        "    M201 --> M202 --> M203 --> M204"
    )
    system_mmd = extract_first_mermaid_block(system_block) or (
        "graph LR\n"
        "    C[客户端] --> G[网关服务]\n"
        "    G --> S[核心处理服务]\n"
        "    S --> D[(存储系统)]\n"
        "    S --> M[监控与告警系统]"
    )

    flow_path = session_dir / "05_diagrams" / "flowcharts" / "method_flowchart.mmd"
    device_path = session_dir / "05_diagrams" / "structural_diagrams" / "device_structure.mmd"
    system_path = session_dir / "05_diagrams" / "sequence_diagrams" / "system_architecture.mmd"

    write_text(flow_path, flow_mmd.strip() + "\n")
    write_text(device_path, device_mmd.strip() + "\n")
    write_text(system_path, system_mmd.strip() + "\n")

    figures_md = "\n".join(
        [
            "## 附图清单",
            "",
            "- 图1：方法流程图（`05_diagrams/flowcharts/method_flowchart.mmd`）",
            "- 图2：装置结构图（`05_diagrams/structural_diagrams/device_structure.mmd`）",
            "- 图3：系统架构图（`05_diagrams/sequence_diagrams/system_architecture.mmd`）",
            "",
        ]
    )
    write_text(session_dir / "04_content" / "figures.md", figures_md)


def stage_markdown_merger(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})

    title = str(parsed_info.get("title", "一种数据处理方法、装置、设备及存储介质")).strip()

    abstract_md = read_text(session_dir / "04_content" / "abstract.md")
    claims_md = read_text(session_dir / "04_content" / "claims.md")
    description_md = read_text(session_dir / "04_content" / "description.md")

    flow_mmd = read_text(session_dir / "05_diagrams" / "flowcharts" / "method_flowchart.mmd")
    device_mmd = read_text(session_dir / "05_diagrams" / "structural_diagrams" / "device_structure.mmd")
    system_mmd = read_text(session_dir / "05_diagrams" / "sequence_diagrams" / "system_architecture.mmd")

    final_md = f"""# {title}

## 目录
1. 说明书摘要
2. 权利要求书
3. 说明书
4. 附图

---

## 说明书摘要

{abstract_md.strip()}

---

## 权利要求书

{claims_md.strip()}

---

## 说明书

{description_md.strip()}

---

## 附图

### 图1 方法流程图

```mermaid
{flow_mmd.strip()}
```

### 图2 装置结构图

```mermaid
{device_mmd.strip()}
```

### 图3 系统架构图

```mermaid
{system_mmd.strip()}
```
"""

    final_dir = session_dir / "06_final"
    final_dir.mkdir(parents=True, exist_ok=True)
    write_text(final_dir / "complete_patent.md", final_md.strip() + "\n")

    description_len = len(re.sub(r"\s+", "", description_md))
    summary_md = "\n".join(
        [
            "# 生成摘要",
            "",
            f"- session_id: {ctx['session_id']}",
            f"- runtime_backend: {ctx['runtime_backend']}",
            f"- generated_at: {datetime.now().isoformat(timespec='seconds')}",
            f"- description_characters: {description_len}",
            "- required_description_characters: 10000",
            f"- meets_description_requirement: {'yes' if description_len >= 10000 else 'no'}",
            "",
        ]
    )
    write_text(final_dir / "summary_report.md", summary_md)


def validate_stage_outputs(session_dir: Path, stage_name: str) -> None:
    required_outputs: Dict[str, List[Path]] = {
        "input-parser": [session_dir / "01_input" / "parsed_info.json"],
        "patent-searcher": [
            session_dir / "02_research" / "similar_patents.json",
            session_dir / "02_research" / "prior_art_analysis.md",
            session_dir / "02_research" / "writing_style_guide.md",
        ],
        "outline-generator": [
            session_dir / "03_outline" / "patent_outline.md",
            session_dir / "03_outline" / "structure_mapping.json",
        ],
        "abstract-writer": [session_dir / "04_content" / "abstract.md"],
        "claims-writer": [session_dir / "04_content" / "claims.md"],
        "description-writer": [session_dir / "04_content" / "description.md"],
        "diagram-generator": [
            session_dir / "05_diagrams" / "flowcharts" / "method_flowchart.mmd",
            session_dir / "05_diagrams" / "structural_diagrams" / "device_structure.mmd",
            session_dir / "05_diagrams" / "sequence_diagrams" / "system_architecture.mmd",
        ],
        "markdown-merger": [session_dir / "06_final" / "complete_patent.md"],
    }

    expected = required_outputs.get(stage_name, [])
    missing = [str(path) for path in expected if not path.exists()]
    if missing:
        raise RuntimeError(f"Stage {stage_name} missing outputs: {', '.join(missing)}")


def run_stage_with_retry(
    ctx: Dict[str, Any],
    stage_name: str,
    stage_fn: Any,
    *,
    max_retries: int,
) -> None:
    session_dir: Path = ctx["session_dir"]
    error_log_path = session_dir / f"{stage_name}_error.log"

    for attempt in range(1, max_retries + 1):
        log(f"[{stage_name}] attempt {attempt}/{max_retries} started")
        try:
            stage_fn(ctx)
            validate_stage_outputs(session_dir, stage_name)
            log(f"[{stage_name}] completed")
            return
        except Exception as exc:  # noqa: BLE001
            trace = traceback.format_exc()
            log(f"[{stage_name}] failed on attempt {attempt}: {exc}")
            with error_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n=== [{datetime.now().isoformat(timespec='seconds')}] attempt {attempt}/{max_retries} ===\n"
                )
                handle.write(trace)
                if not trace.endswith("\n"):
                    handle.write("\n")
            if attempt >= max_retries:
                raise


def build_stage_plan() -> List[Tuple[str, Any]]:
    return [
        ("input-parser", stage_input_parser),
        ("patent-searcher", stage_patent_searcher),
        ("outline-generator", stage_outline_generator),
        ("abstract-writer", stage_abstract_writer),
        ("claims-writer", stage_claims_writer),
        ("description-writer", stage_description_writer),
        ("diagram-generator", stage_diagram_generator),
        ("markdown-merger", stage_markdown_merger),
    ]


def prepare_workspace(session_id: str, input_path: Path) -> Path:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = OUTPUT_DIR / f"temp_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    for folder in [
        "01_input",
        "02_research",
        "03_outline",
        "04_content",
        "05_diagrams/flowcharts",
        "05_diagrams/structural_diagrams",
        "05_diagrams/sequence_diagrams",
        "06_final",
        "metadata",
    ]:
        (session_dir / folder).mkdir(parents=True, exist_ok=True)

    return session_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run patent writing pipeline without external AI CLI")
    parser.add_argument("--session-id", required=True, help="UUID session id")
    parser.add_argument("--input-path", required=True, help="Input disclosure .docx path")
    parser.add_argument(
        "--runtime-backend",
        default=DEFAULT_RUNTIME_BACKEND,
        choices=["anthropic", "openai"],
        help="Runtime backend for direct API calls",
    )
    parser.add_argument(
        "--task-prompt",
        default="",
        help="Optional prompt override / extra constraints",
    )
    parser.add_argument(
        "--max-stage-retries",
        type=int,
        default=3,
        help="Maximum retries for each stage",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    runtime_backend = args.runtime_backend
    if not is_runtime_available(runtime_backend):
        hint = runtime_setup_hint(runtime_backend)
        raise SystemExit(f"Runtime backend '{runtime_backend}' is not ready. {hint}")

    input_path = Path(args.input_path).expanduser()
    if not input_path.is_absolute():
        input_path = ROOT_DIR / input_path

    session_dir = prepare_workspace(args.session_id, input_path)

    log(f"Session: {args.session_id}")
    log(f"Runtime backend: {runtime_backend} ({get_runtime_label(runtime_backend)})")
    log(f"Input: {input_path}")
    log(f"Workspace: {session_dir}")

    context: Dict[str, Any] = {
        "session_id": args.session_id,
        "runtime_backend": runtime_backend,
        "input_path": input_path,
        "session_dir": session_dir,
        "task_prompt": args.task_prompt,
    }

    plan = build_stage_plan()
    for stage_name, stage_fn in plan:
        run_stage_with_retry(
            context,
            stage_name,
            stage_fn,
            max_retries=max(1, int(args.max_stage_retries)),
        )

    final_path = session_dir / "06_final" / "complete_patent.md"
    log(f"Pipeline completed successfully. Final output: {final_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeClientError as exc:
        log(f"Runtime error: {exc}")
        raise SystemExit(2) from exc
    except Exception as exc:  # noqa: BLE001
        log(f"Unhandled pipeline failure: {exc}")
        traceback.print_exc()
        raise SystemExit(1) from exc
