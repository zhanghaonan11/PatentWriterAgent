#!/usr/bin/env python3
"""Native Python patent pipeline runner without external AI CLI runtimes."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"[\n,，;；]", value) if s.strip()]
    return []


def trim_text(text: str, limit: int = 20000) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit // 2] + "\n\n...[truncated]...\n\n" + value[-(limit // 2) :]


def extract_json(text: str) -> Optional[Any]:
    raw = (text or "").strip()
    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    match = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def extract_block(text: str, begin: str, end: str) -> str:
    m = re.search(re.escape(begin) + r"(.*?)" + re.escape(end), text, flags=re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


def load_agent_instruction(name: str) -> str:
    return read_text(AGENTS_DIR / f"{name}.md")


def llm(runtime_backend: str, prompt: str, *, max_tokens: int, temperature: float, timeout_seconds: int = 900) -> str:
    return generate_text(
        runtime_backend=runtime_backend,
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )


def normalize_parsed_info(payload: Dict[str, Any]) -> Dict[str, Any]:
    title = str(payload.get("title", "")).strip() or "一种数据处理方法、装置、设备及存储介质"
    technical_problem = str(payload.get("technical_problem", "")).strip() or "解决处理效率与资源利用率问题。"
    technical_solution = str(payload.get("technical_solution", "")).strip() or "通过分层流程与自适应策略完成处理。"

    existing_solutions = normalize_list(payload.get("existing_solutions"))
    if not existing_solutions:
        existing_solutions = ["集中式规则处理方案", "固定流程批处理方案"]

    existing_drawbacks = normalize_list(payload.get("existing_drawbacks"))
    if not existing_drawbacks:
        existing_drawbacks = ["扩展性不足", "异常恢复能力弱"]

    benefits = normalize_list(payload.get("benefits"))
    if not benefits:
        benefits = ["吞吐能力提升", "故障恢复时间缩短", "资源利用率提升"]

    keywords = normalize_list(payload.get("keywords"))
    if not keywords:
        keywords = ["数据处理", "调度", "异常恢复", "并发", "资源优化"]

    return {
        "title": title,
        "technical_problem": technical_problem,
        "existing_solutions": existing_solutions,
        "existing_drawbacks": existing_drawbacks,
        "technical_solution": technical_solution,
        "benefits": benefits,
        "keywords": keywords,
    }


def stage_input_parser(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    input_path: Path = ctx["input_path"]
    runtime_backend = ctx["runtime_backend"]

    stage_dir = session_dir / "01_input"
    stage_dir.mkdir(parents=True, exist_ok=True)

    raw_docx = stage_dir / "raw_document.docx"
    shutil.copy2(input_path, raw_docx)

    markdown = MarkItDown().convert(str(raw_docx)).text_content or ""
    markdown = markdown.strip()
    if not markdown:
        raise RuntimeError("failed to parse input docx")

    write_text(stage_dir / "raw_document.md", markdown)

    instruction = trim_text(load_agent_instruction("input-parser"), 6000)
    prompt = f"""请根据交底书内容提取结构化信息，只输出 JSON：
{{
  "title": "",
  "technical_problem": "",
  "existing_solutions": [""],
  "existing_drawbacks": [""],
  "technical_solution": "",
  "benefits": [""],
  "keywords": [""]
}}

要求：keywords 5-10 个，所有列表字段必须为数组。

参考指令：
{instruction}

文档内容：
{trim_text(markdown, 30000)}
"""

    out = llm(runtime_backend, prompt, max_tokens=2200, temperature=0.1)
    parsed = extract_json(out)
    if not isinstance(parsed, dict):
        raise RuntimeError("input-parser returned invalid JSON")
    write_json(stage_dir / "parsed_info.json", normalize_parsed_info(parsed))


def stage_patent_searcher(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    runtime_backend = ctx["runtime_backend"]

    parsed_info = read_json(session_dir / "01_input" / "parsed_info.json", {})
    keywords = normalize_list(parsed_info.get("keywords"))[:6]
    if not keywords:
        keywords = ["数据处理", "并发调度", "状态同步"]

    instruction = trim_text(load_agent_instruction("patent-searcher"), 6000)
    prompt = f"""你是专利检索分析助手。在不可直接访问外部检索时，请基于关键词给出可参考专利方向。
输出格式：
<<<SIMILAR_PATENTS_JSON>>>
[{{"title":"","publication_no":"","country":"CN","relevance":0.8,"key_points":[""],"analysis":""}}]
<<<END_SIMILAR_PATENTS_JSON>>>
<<<PRIOR_ART_ANALYSIS_MD>>>
# 现有技术分析
...
<<<END_PRIOR_ART_ANALYSIS_MD>>>
<<<WRITING_STYLE_GUIDE_MD>>>
# 写作风格建议
...
<<<END_WRITING_STYLE_GUIDE_MD>>>

关键词：{', '.join(keywords)}
参考指令：
{instruction}
"""

    out = llm(runtime_backend, prompt, max_tokens=3000, temperature=0.2)
    similar_json = extract_json(extract_block(out, "<<<SIMILAR_PATENTS_JSON>>>", "<<<END_SIMILAR_PATENTS_JSON>>>"))
    analysis_md = extract_block(out, "<<<PRIOR_ART_ANALYSIS_MD>>>", "<<<END_PRIOR_ART_ANALYSIS_MD>>>")
    style_md = extract_block(out, "<<<WRITING_STYLE_GUIDE_MD>>>", "<<<END_WRITING_STYLE_GUIDE_MD>>>")

    similar: List[Dict[str, Any]] = []
    if isinstance(similar_json, list):
        for item in similar_json[:10]:
            if not isinstance(item, dict):
                continue
            similar.append(
                {
                    "title": str(item.get("title", "未命名参考专利")).strip() or "未命名参考专利",
                    "publication_no": str(item.get("publication_no", "N/A")).strip() or "N/A",
                    "country": str(item.get("country", "CN")).strip() or "CN",
                    "relevance": float(item.get("relevance", 0.6)),
                    "key_points": normalize_list(item.get("key_points"))[:6],
                    "analysis": str(item.get("analysis", "")).strip(),
                }
            )

    if not similar:
        similar = [
            {
                "title": f"面向{kw}的改进型技术方案",
                "publication_no": f"CN-REF-{i+1:03d}",
                "country": "CN",
                "relevance": 0.65,
                "key_points": ["流程模块化", "可扩展处理", "稳定性增强"],
                "analysis": "用于学习写作风格与结构组织。",
            }
            for i, kw in enumerate(keywords[:5])
        ]

    if not analysis_md:
        analysis_md = "# 现有技术分析\n\n未获取到外部检索结果，已给出主题相关方向与对比思路。"
    if not style_md:
        style_md = "# 写作风格建议\n\n- 法律化客观表达\n- 步骤与模块编号对应\n- 术语保持一致"

    stage_dir = session_dir / "02_research"
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_json(stage_dir / "similar_patents.json", similar)
    write_text(stage_dir / "prior_art_analysis.md", analysis_md.strip() + "\n")
    write_text(stage_dir / "writing_style_guide.md", style_md.strip() + "\n")


def stage_outline_generator(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    runtime_backend = ctx["runtime_backend"]

    parsed = read_json(session_dir / "01_input" / "parsed_info.json", {})
    similar = read_json(session_dir / "02_research" / "similar_patents.json", [])
    guide = trim_text(read_text(PATENT_GUIDE_PATH), 12000)
    skill = trim_text(read_text(PATENT_SKILL_PATH), 12000)
    instruction = trim_text(load_agent_instruction("outline-generator"), 6000)

    prompt = f"""请输出两个区块：
<<<PATENT_OUTLINE_MD>>>
# 专利大纲
...
<<<END_PATENT_OUTLINE_MD>>>
<<<STRUCTURE_MAPPING_JSON>>>
{{"patent_title":"","sections":[]}}
<<<END_STRUCTURE_MAPPING_JSON>>>

要求：必须包含摘要、权利要求、说明书各章；具体实施方式 min_words >=10000。

参考指令：
{instruction}

技能规范（节选）：
{skill}

写作指南（节选）：
{guide}

输入：
parsed={json.dumps(parsed, ensure_ascii=False, indent=2)}
similar={json.dumps(similar[:6], ensure_ascii=False, indent=2)}
"""

    out = llm(runtime_backend, prompt, max_tokens=4200, temperature=0.2)
    outline_md = extract_block(out, "<<<PATENT_OUTLINE_MD>>>", "<<<END_PATENT_OUTLINE_MD>>>")
    mapping_json = extract_json(extract_block(out, "<<<STRUCTURE_MAPPING_JSON>>>", "<<<END_STRUCTURE_MAPPING_JSON>>>"))

    if not outline_md:
        outline_md = "# 专利大纲\n\n- 说明书摘要（<=300字）\n- 权利要求书\n- 说明书（技术领域、背景技术、发明内容、附图说明、具体实施方式>10000字）"
    if not isinstance(mapping_json, dict):
        mapping_json = {
            "patent_title": str(parsed.get("title", "一种数据处理方法、装置、设备及存储介质")),
            "sections": [
                {"id": "01_abstract", "title": "说明书摘要", "max_words": 300},
                {"id": "02_claims", "title": "权利要求书"},
                {"id": "03_tech_field", "title": "技术领域", "min_words": 200},
                {"id": "03_background", "title": "背景技术", "min_words": 1000},
                {"id": "03_summary", "title": "发明内容", "min_words": 1500},
                {"id": "03_drawings", "title": "附图说明", "min_words": 300},
                {"id": "03_embodiments", "title": "具体实施方式", "min_words": 10000},
            ],
        }

    stage_dir = session_dir / "03_outline"
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_text(stage_dir / "patent_outline.md", outline_md.strip() + "\n")
    write_json(stage_dir / "structure_mapping.json", mapping_json)


def stage_abstract_writer(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    runtime_backend = ctx["runtime_backend"]

    parsed = read_json(session_dir / "01_input" / "parsed_info.json", {})
    outline_md = read_text(session_dir / "03_outline" / "patent_outline.md")
    instruction = trim_text(load_agent_instruction("abstract-writer"), 5000)

    prompt = f"""请撰写中文专利摘要，要求：
1) 必须以“本申请公开了”开头
2) 不超过300字
3) 包含技术问题、技术方案、有益效果
4) 仅输出摘要正文

参考指令：
{instruction}

输入：
{json.dumps(parsed, ensure_ascii=False, indent=2)}

大纲：
{trim_text(outline_md, 10000)}
"""

    out = llm(runtime_backend, prompt, max_tokens=900, temperature=0.1)
    abstract = out.strip()
    if not abstract.startswith("本申请公开了"):
        abstract = "本申请公开了" + abstract.lstrip("，,。 .")
    if len(abstract) > 320:
        abstract = abstract[:320].rstrip() + "。"

    write_text(session_dir / "04_content" / "abstract.md", abstract + "\n")


def stage_claims_writer(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    runtime_backend = ctx["runtime_backend"]

    parsed = read_json(session_dir / "01_input" / "parsed_info.json", {})
    outline_md = read_text(session_dir / "03_outline" / "patent_outline.md")
    abstract_md = read_text(session_dir / "04_content" / "abstract.md")
    instruction = trim_text(load_agent_instruction("claims-writer"), 6000)

    prompt = f"""请输出专利权利要求书 Markdown，要求：
- 包含方法独权+从权，装置独权+从权，设备独权，介质独权
- 方法权利要求步骤用分号（；）分隔
- 句式符合法律文本
- 只输出最终 Markdown

参考指令：
{instruction}

输入：
parsed={json.dumps(parsed, ensure_ascii=False, indent=2)}

abstract={abstract_md}

outline={trim_text(outline_md, 10000)}
"""

    out = llm(runtime_backend, prompt, max_tokens=4000, temperature=0.2).strip()
    if not re.search(r"^\s*1\.", out, flags=re.MULTILINE):
        out = (
            "1. 一种数据处理方法，其特征在于，包括：\n"
            "获取待处理数据；\n"
            "执行目标处理流程；\n"
            "输出处理结果。\n\n" + out
        )

    write_text(session_dir / "04_content" / "claims.md", out + "\n")


def _generate_long_section(runtime_backend: str, heading: str, min_chars: int, context: str) -> str:
    prompt = f"""请仅输出“{heading}”章节正文。
要求：
- 术语一致
- 中文技术写作
- 最少 {min_chars} 个中文字符

上下文：
{context}
"""

    text = llm(runtime_backend, prompt, max_tokens=3200, temperature=0.25, timeout_seconds=1200).strip()
    if len(re.sub(r"\s+", "", text)) < min_chars:
        expand = f"请在不改变技术逻辑前提下扩写到至少 {min_chars} 个中文字符，仅输出最终正文：\n{text}"
        text = llm(runtime_backend, expand, max_tokens=3200, temperature=0.3, timeout_seconds=1200).strip()
    return text


def stage_description_writer(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    runtime_backend = ctx["runtime_backend"]

    parsed = read_json(session_dir / "01_input" / "parsed_info.json", {})
    outline = read_text(session_dir / "03_outline" / "patent_outline.md")
    abstract = read_text(session_dir / "04_content" / "abstract.md")
    claims = read_text(session_dir / "04_content" / "claims.md")
    prior = read_text(session_dir / "02_research" / "prior_art_analysis.md")
    skill = trim_text(read_text(PATENT_SKILL_PATH), 12000)
    guide = trim_text(read_text(PATENT_GUIDE_PATH), 12000)
    instruction = trim_text(load_agent_instruction("description-writer"), 7000)

    ctx_text = trim_text(
        "\n\n".join(
            [
                f"parsed={json.dumps(parsed, ensure_ascii=False, indent=2)}",
                f"outline={outline}",
                f"abstract={abstract}",
                f"claims={claims}",
                f"prior={prior}",
                f"skill={skill}",
                f"guide={guide}",
                f"instruction={instruction}",
            ]
        ),
        30000,
    )

    tech = _generate_long_section(runtime_backend, "技术领域", 220, ctx_text)
    bg = _generate_long_section(runtime_backend, "背景技术", 1500, ctx_text)
    summary = _generate_long_section(runtime_backend, "发明内容", 1800, ctx_text)
    drawing = _generate_long_section(runtime_backend, "附图说明", 360, ctx_text)
    impl_a = _generate_long_section(runtime_backend, "具体实施方式（实施例一）", 3600, ctx_text)
    impl_b = _generate_long_section(runtime_backend, "具体实施方式（实施例二）", 3600, ctx_text)

    description = (
        "## 技术领域\n\n"
        + tech
        + "\n\n## 背景技术\n\n"
        + bg
        + "\n\n## 发明内容\n\n"
        + summary
        + "\n\n## 附图说明\n\n"
        + drawing
        + "\n\n## 具体实施方式\n\n"
        + impl_a
        + "\n\n"
        + impl_b
        + "\n"
    )

    if len(re.sub(r"\s+", "", description)) < 10000:
        expand = (
            "以下说明书长度不足，请扩写‘具体实施方式’使总长度超过10000中文字符。"
            "输出完整 Markdown：\n\n" + description
        )
        expanded = llm(runtime_backend, expand, max_tokens=3800, temperature=0.25, timeout_seconds=1200).strip()
        if expanded:
            description = expanded

    write_text(session_dir / "04_content" / "description.md", description.strip() + "\n")


def _extract_mermaid_block(text: str) -> str:
    m = re.search(r"```mermaid\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).strip()


def stage_diagram_generator(ctx: Dict[str, Any]) -> None:
    session_dir: Path = ctx["session_dir"]
    runtime_backend = ctx["runtime_backend"]

    description = read_text(session_dir / "04_content" / "description.md")
    mapping = read_json(session_dir / "03_outline" / "structure_mapping.json", {})
    instruction = trim_text(load_agent_instruction("diagram-generator"), 5000)

    prompt = f"""请输出三段 Mermaid，格式如下：
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

要求：流程图 graph TD；装置图 graph TB；系统图 graph LR。

参考指令：
{instruction}

输入 mapping：{json.dumps(mapping, ensure_ascii=False, indent=2)}
输入 description：{trim_text(description, 15000)}
"""

    out = llm(runtime_backend, prompt, max_tokens=2400, temperature=0.2)

    flow = _extract_mermaid_block(extract_block(out, "<<<FLOWCHART_MERMAID>>>", "<<<END_FLOWCHART_MERMAID>>>"))
    device = _extract_mermaid_block(extract_block(out, "<<<DEVICE_MERMAID>>>", "<<<END_DEVICE_MERMAID>>>"))
    system = _extract_mermaid_block(extract_block(out, "<<<SYSTEM_MERMAID>>>", "<<<END_SYSTEM_MERMAID>>>"))

    if not flow:
        flow = "graph TD\n    A[S101: 获取数据] --> B[S102: 处理数据] --> C[S103: 输出结果]"
    if not device:
        device = "graph TB\n    M201[获取模块 201] --> M202[处理模块 202] --> M203[输出模块 203]"
    if not system:
        system = "graph LR\n    C[客户端] --> S[服务节点] --> D[(存储系统)]"

    write_text(session_dir / "05_diagrams" / "flowcharts" / "method_flowchart.mmd", flow + "\n")
    write_text(session_dir / "05_diagrams" / "structural_diagrams" / "device_structure.mmd", device + "\n")
    write_text(session_dir / "05_diagrams" / "sequence_diagrams" / "system_architecture.mmd", system + "\n")

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
    parsed = read_json(session_dir / "01_input" / "parsed_info.json", {})

    title = str(parsed.get("title", "一种数据处理方法、装置、设备及存储介质")).strip()
    abstract = read_text(session_dir / "04_content" / "abstract.md")
    claims = read_text(session_dir / "04_content" / "claims.md")
    description = read_text(session_dir / "04_content" / "description.md")
    flow = read_text(session_dir / "05_diagrams" / "flowcharts" / "method_flowchart.mmd")
    device = read_text(session_dir / "05_diagrams" / "structural_diagrams" / "device_structure.mmd")
    system = read_text(session_dir / "05_diagrams" / "sequence_diagrams" / "system_architecture.mmd")

    final_md = f"""# {title}

## 目录
1. 说明书摘要
2. 权利要求书
3. 说明书
4. 附图

---

## 说明书摘要

{abstract.strip()}

---

## 权利要求书

{claims.strip()}

---

## 说明书

{description.strip()}

---

## 附图

### 图1 方法流程图

```mermaid
{flow.strip()}
```

### 图2 装置结构图

```mermaid
{device.strip()}
```

### 图3 系统架构图

```mermaid
{system.strip()}
```
"""

    final_dir = session_dir / "06_final"
    final_dir.mkdir(parents=True, exist_ok=True)
    write_text(final_dir / "complete_patent.md", final_md.strip() + "\n")

    description_len = len(re.sub(r"\s+", "", description))
    summary = "\n".join(
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
    write_text(final_dir / "summary_report.md", summary)


def validate_stage_outputs(session_dir: Path, stage: str) -> None:
    required: Dict[str, List[Path]] = {
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

    missing = [str(p) for p in required.get(stage, []) if not p.exists()]
    if missing:
        raise RuntimeError(f"Stage {stage} missing outputs: {', '.join(missing)}")


def run_stage(
    ctx: Dict[str, Any],
    stage_name: str,
    fn: Callable[[Dict[str, Any]], None],
    retries: int,
) -> None:
    session_dir: Path = ctx["session_dir"]
    error_log = session_dir / f"{stage_name}_error.log"

    for attempt in range(1, retries + 1):
        log(f"[{stage_name}] attempt {attempt}/{retries} started")
        try:
            fn(ctx)
            validate_stage_outputs(session_dir, stage_name)
            log(f"[{stage_name}] completed")
            return
        except Exception as exc:  # noqa: BLE001
            trace = traceback.format_exc()
            log(f"[{stage_name}] failed on attempt {attempt}: {exc}")
            with error_log.open("a", encoding="utf-8") as h:
                h.write(f"\n=== [{datetime.now().isoformat(timespec='seconds')}] attempt {attempt}/{retries} ===\n")
                h.write(trace)
                if not trace.endswith("\n"):
                    h.write("\n")
            if attempt >= retries:
                raise


def stage_plan() -> List[Tuple[str, Callable[[Dict[str, Any]], None]]]:
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
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--runtime-backend", default=DEFAULT_RUNTIME_BACKEND, choices=["anthropic", "openai"])
    parser.add_argument("--task-prompt", default="")
    parser.add_argument("--max-stage-retries", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    backend = args.runtime_backend
    if not is_runtime_available(backend):
        raise SystemExit(f"Runtime backend '{backend}' is not ready. {runtime_setup_hint(backend)}")

    input_path = Path(args.input_path).expanduser()
    if not input_path.is_absolute():
        input_path = ROOT_DIR / input_path

    session_dir = prepare_workspace(args.session_id, input_path)

    log(f"Session: {args.session_id}")
    log(f"Runtime backend: {backend} ({get_runtime_label(backend)})")
    log(f"Input: {input_path}")
    log(f"Workspace: {session_dir}")

    ctx: Dict[str, Any] = {
        "session_id": args.session_id,
        "runtime_backend": backend,
        "input_path": input_path,
        "session_dir": session_dir,
        "task_prompt": args.task_prompt,
    }

    retries = max(1, int(args.max_stage_retries))
    for name, fn in stage_plan():
        run_stage(ctx, name, fn, retries)

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
