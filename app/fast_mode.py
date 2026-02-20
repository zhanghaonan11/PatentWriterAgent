"""Fast Mode: expand invention idea into disclosure, then feed to pipeline."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import (
    DATA_DIR,
    FAST_SECTION_TITLES,
    EXEC_MODE_CLI,
    ROOT_DIR,
)
from app.utils import normalize_newlines, to_display_path, xml_escape
from app.session import append_log_event
from app.backend import (
    get_cli_label,
    safe_runtime_label,
)
from runtime_client import RuntimeClientError, generate_text


def build_fast_mode_prompt(invention_idea: str) -> str:
    idea = normalize_newlines(invention_idea).strip()
    required_sections = "\n".join(f"- {title}" for title in FAST_SECTION_TITLES)
    return (
        "你是一名资深中国专利代理人。请把给定的发明构思扩写为可用于专利写作的技术交底草稿。\n\n"
        "输出要求：\n"
        "1. 仅输出中文 Markdown 正文，不要输出解释、前言或额外说明。\n"
        "2. 必须包含以下章节，并保持该顺序：\n"
        f"{required_sections}\n"
        '3. 每个章节都要给出具体技术内容，避免空泛表述。参数不明确时可合理假设，并显式标注"假设：..."。\n'
        "4. 适度补充实施细节（结构、流程、关键参数范围、可选方案），使内容可直接用于后续专利生成。\n"
        "5. 输出必须严格包裹在以下标记之间：\n"
        "<FAST_DISCLOSURE_START>\n"
        "...这里是正文...\n"
        "<FAST_DISCLOSURE_END>\n\n"
        "发明构思如下：\n"
        f"{idea}\n"
    )


def extract_text_chunks_from_payload(payload: Dict[str, Any]) -> List[str]:
    chunks: List[str] = []
    event_type = str(payload.get("type", ""))

    if event_type == "assistant":
        message = payload.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        elif isinstance(content, str):
            chunks.append(content)
            
        direct_text = message.get("text")
        if isinstance(direct_text, str):
            chunks.append(direct_text)

    if event_type == "item.completed":
        item = payload.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)

    if event_type == "result":
        result = payload.get("result")
        if isinstance(result, str):
            chunks.append(result)

    if not chunks:
        for key in ("text", "content", "result", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                chunks.append(value)

    return [chunk.strip() for chunk in chunks if isinstance(chunk, str) and chunk.strip()]


def extract_fast_disclosure_text(raw_output: str) -> str:
    json_chunks: List[str] = []
    plain_chunks: List[str] = []

    for line in normalize_newlines(raw_output).split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                plain_chunks.append(stripped)
                continue
            if isinstance(payload, dict):
                json_chunks.extend(extract_text_chunks_from_payload(payload))
            continue

        plain_chunks.append(stripped)

    chunks = json_chunks if json_chunks else plain_chunks
    deduped: List[str] = []
    for chunk in chunks:
        if deduped and chunk == deduped[-1]:
            continue
        deduped.append(chunk)

    merged = "\n\n".join(deduped).strip()

    marker_match = re.search(
        r"<FAST_DISCLOSURE_START>(.*?)<FAST_DISCLOSURE_END>",
        merged,
        flags=re.DOTALL,
    )
    if marker_match:
        return marker_match.group(1).strip()

    fence_match = re.search(
        r"```(?:markdown|md|text)?\s*(.*?)```",
        merged,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence_match:
        return fence_match.group(1).strip()

    return merged


def ensure_fast_disclosure_sections(content: str, invention_idea: str) -> str:
    text = normalize_newlines(content).strip()

    if not text:
        text = (
            "发明名称\n"
            "待补充\n\n"
            "要解决的技术问题\n"
            f"{invention_idea.strip() or '待补充'}"
        )

    missing = [title for title in FAST_SECTION_TITLES if title not in text]
    if missing:
        additions: List[str] = []
        for title in missing:
            if title == "要解决的技术问题":
                placeholder = invention_idea.strip() or "待补充"
            else:
                placeholder = "待补充（假设：后续补齐具体参数、结构和实施细节）"
            additions.append(f"{title}\n{placeholder}")

        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + "\n\n".join(additions)

    return text.strip() + "\n"


def write_simple_docx(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    paragraphs: List[str] = []
    for line in normalize_newlines(content).split("\n"):
        escaped = xml_escape(line)
        if not escaped:
            paragraphs.append("<w:p/>")
            continue
        paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{escaped}</w:t></w:r></w:p>')

    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(paragraphs)}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
"""

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Fast Mode Disclosure</dc:title>
  <dc:creator>PatentWriterAgent</dc:creator>
  <cp:lastModifiedBy>PatentWriterAgent</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>PatentWriterAgent</Application>
</Properties>
"""

    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("word/document.xml", document_xml)


def generate_fast_disclosure_once(
    runtime_backend: str,
    invention_idea: str,
    timeout_seconds: int = 300,
) -> Tuple[bool, str, str]:
    prompt = build_fast_mode_prompt(invention_idea)
    try:
        output = generate_text(
            runtime_backend=runtime_backend,
            prompt=prompt,
            max_tokens=2200,
            temperature=0.3,
            timeout_seconds=timeout_seconds,
        )
    except RuntimeClientError as exc:
        return False, "", str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, "", f"Runtime call failed during fast-mode preprocessing: {exc}"
    return True, normalize_newlines(output), ""


def run_cli_once(
    cli_backend: str,
    session_id: str,
    prompt: str,
    timeout_seconds: int = 300,
) -> Tuple[bool, str, str, List[str]]:
    from app.backend import build_cli_command

    command = build_cli_command(cli_backend, session_id, prompt, fast_mode=True)
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_output_raw: Any = exc.stdout or ""
        if isinstance(timeout_output_raw, bytes):
            timeout_output = timeout_output_raw.decode("utf-8", errors="replace")
        else:
            timeout_output = str(timeout_output_raw)
        return (
            False,
            normalize_newlines(timeout_output),
            f"极速模式预处理期间 {get_cli_label(cli_backend)} 请求超时 ({timeout_seconds} 秒)。",
            command,
        )

    output = normalize_newlines(completed.stdout or "")
    if completed.returncode != 0:
        return (
            False,
            output,
            f"极速模式预处理进程异常退出，状态码: {completed.returncode}",
            command,
        )

    return True, output, "", command


def prepare_fast_mode_input(
    session_id: str,
    execution_mode: str,
    runtime_backend: str,
    cli_backend: str,
    invention_idea: str,
) -> Tuple[bool, str, Optional[Path]]:
    idea = invention_idea.strip()
    if not idea:
        return False, "极速模式需要提供详细的发明构思。", None

    prompt = build_fast_mode_prompt(idea)

    if execution_mode == EXEC_MODE_CLI:
        ok, raw_output, error_message, command = run_cli_once(
            cli_backend=cli_backend,
            session_id=str(uuid.uuid4()),
            prompt=prompt,
        )
        append_log_event(
            session_id,
            "极速模式预处理开始",
            f"Command: {' '.join(shlex.quote(item) for item in command)}",
        )
    else:
        ok, raw_output, error_message = generate_fast_disclosure_once(
            runtime_backend=runtime_backend,
            invention_idea=idea,
        )
        append_log_event(
            session_id,
            "极速模式预处理开始",
            f"Runtime backend: {safe_runtime_label(runtime_backend)}",
        )

    if raw_output.strip():
        append_log_event(session_id, "预处理输出内容", raw_output)

    if not ok:
        append_log_event(session_id, "预处理失败", error_message)
        return False, error_message, None

    expanded_text = extract_fast_disclosure_text(raw_output)
    expanded_text = ensure_fast_disclosure_sections(expanded_text, idea)

    if len(expanded_text.strip()) < 80:
        message = (
            "极速模式生成的交底书内容过少。\n"
            "请提供更多细节后重试。"
        )
        append_log_event(session_id, "预处理失败", message)
        return False, message, None

    target_dir = DATA_DIR / "uploads" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = target_dir / "fast_disclosure.md"
    docx_path = target_dir / "fast_disclosure.docx"

    markdown_path.write_text(expanded_text, encoding="utf-8")
    write_simple_docx(docx_path, expanded_text)

    append_log_event(
        session_id,
        "极速模式预处理完成",
        f"生成文件：{to_display_path(markdown_path)}\n生成文件：{to_display_path(docx_path)}",
    )

    return True, f"极速模式成功生成技术交底书文件：{to_display_path(docx_path)}", docx_path
