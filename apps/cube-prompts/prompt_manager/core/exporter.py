"""Export formatters for prompts into LLM API structures, frameworks, CSV, and portable files."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import uuid

from prompt_manager.core.models import Prompt


def to_plain_text(prompt: Prompt, hydrated_content: str = "") -> str:
    """Format prompt into plain text, incorporating system instruction if present."""
    content = hydrated_content if hydrated_content else prompt.template_content
    if prompt.system_instruction.strip():
        return f"[SYSTEM INSTRUCTION]\n{prompt.system_instruction.strip()}\n\n[USER PROMPT]\n{content.strip()}"
    return content.strip()


def to_openai_payload(prompt: Prompt, hydrated_content: str = "") -> Dict[str, Any]:
    """Format prompt into OpenAI-compatible Chat Completion JSON payload."""
    content = hydrated_content if hydrated_content else prompt.template_content
    messages = []

    if prompt.system_instruction.strip():
        messages.append({"role": "system", "content": prompt.system_instruction.strip()})

    messages.append({"role": "user", "content": content.strip()})

    payload: Dict[str, Any] = {
        "model": prompt.target_model if prompt.target_model != "General" else "gpt-4o",
        "messages": messages,
        "temperature": prompt.temperature,
    }
    return payload


def _anthropic_model_id(target_model: str) -> str:
    """Map a target model to a Claude API id: 'Claude Sonnet 5.5' -> 'claude-sonnet-5-5'.

    Ids pass through unchanged; anything that is not a Claude model uses the
    current default. Current models reject temperature, so the payload omits it.
    """
    text = (target_model or "").strip()
    if "claude" not in text.lower():
        return "claude-opus-5-5"
    if " " not in text:
        return text
    return "-".join(text.lower().replace(".", " ").split())


def to_anthropic_payload(prompt: Prompt, hydrated_content: str = "") -> Dict[str, Any]:
    """Format prompt into Anthropic Messages API JSON payload."""
    content = hydrated_content if hydrated_content else prompt.template_content
    payload: Dict[str, Any] = {
        "model": _anthropic_model_id(prompt.target_model),
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": content.strip()}],
    }
    if prompt.system_instruction.strip():
        payload["system"] = prompt.system_instruction.strip()

    return payload


def to_markdown_frontmatter(prompt: Prompt, hydrated_content: str = "") -> str:
    """Format prompt as Markdown with YAML frontmatter.

    LOSSY human export by design: preserves title/target_model/temperature/
    tags/description/system/content but drops id/folder_id/template_id/
    use_count/is_favorite. Output must NOT be reimported as data.
    """
    tags_str = ", ".join(prompt.tags)
    content = hydrated_content if hydrated_content else prompt.template_content

    lines = [
        "---",
        f"title: {json.dumps(prompt.title)}",
        f"target_model: {json.dumps(prompt.target_model)}",
        f"temperature: {prompt.temperature}",
        f"tags: [{tags_str}]",
        f"created_at: {prompt.created_at}",
        f"updated_at: {prompt.updated_at}",
        "---",
        "",
    ]

    if prompt.description.strip():
        lines.extend([f"> {prompt.description.strip()}", ""])

    if prompt.system_instruction.strip():
        lines.extend([
            "## System Instruction",
            "",
            prompt.system_instruction.strip(),
            "",
        ])

    lines.extend([
        "## Prompt",
        "",
        content.strip(),
        "",
    ])

    return "\n".join(lines)


def _escape_triple_quotes(text: str) -> str:
    """Safely escape text for inclusion in a Python triple-quoted string literal."""
    text = text.replace("\\", "\\\\")
    text = text.replace('"""', r'\"\"\"')
    if text.endswith('"'):
        bs_count = 0
        for ch in reversed(text[:-1]):
            if ch == "\\":
                bs_count += 1
            else:
                break
        if bs_count % 2 == 0:
            text = text[:-1] + r'\"'
    return text


def to_langchain_template(prompt: Prompt) -> str:
    """Generate ready-to-run LangChain Python code snippet."""
    # Convert Mustache {{var}} or {{var:default}} to LangChain {var}
    clean_template = re.sub(r"\{\{([a-zA-Z0-9_]+)(?::[^|}]*)?(?:\|[^}]*)?\}\}", r"{\1}", prompt.template_content)
    clean_system = re.sub(r"\{\{([a-zA-Z0-9_]+)(?::[^|}]*)?(?:\|[^}]*)?\}\}", r"{\1}", prompt.system_instruction)

    code = [
        "# LangChain Prompt Template",
        "from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate",
        "",
        "messages = []",
    ]

    if clean_system.strip():
        code.extend([
            f'system_template = """{_escape_triple_quotes(clean_system.strip())}"""',
            "messages.append(SystemMessagePromptTemplate.from_template(system_template))",
        ])

    code.extend([
        f'human_template = """{_escape_triple_quotes(clean_template.strip())}"""',
        "messages.append(HumanMessagePromptTemplate.from_template(human_template))",
        "",
        "prompt = ChatPromptTemplate.from_messages(messages)",
        "# chain = prompt | llm | StrOutputParser()",
    ])

    return "\n".join(code)


def to_llamaindex_template(prompt: Prompt) -> str:
    """Generate ready-to-run LlamaIndex Python code snippet."""
    clean_template = re.sub(r"\{\{([a-zA-Z0-9_]+)(?::[^|}]*)?(?:\|[^}]*)?\}\}", r"{\1}", prompt.template_content)

    return f'''# LlamaIndex Prompt Template
from llama_index.core import PromptTemplate

template_str = """{_escape_triple_quotes(clean_template.strip())}"""
prompt_tmpl = PromptTemplate(template_str)
'''


def to_csv_string(prompts: List[Prompt]) -> str:
    """Export list of prompts as a standard CSV format.

    Includes folder_id/template_id/use_count so JSON is not the only
    lossless path; reader accepts old files lacking these columns.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "title",
        "description",
        "template_content",
        "system_instruction",
        "target_model",
        "temperature",
        "tags",
        "is_favorite",
        "folder_id",
        "template_id",
        "use_count",
        "created_at",
        "updated_at",
    ])

    for p in prompts:
        writer.writerow([
            p.id,
            p.title,
            p.description,
            p.template_content,
            p.system_instruction,
            p.target_model,
            p.temperature,
            ";".join(p.tags),
            1 if p.is_favorite else 0,
            p.folder_id or "",
            p.template_id or "",
            p.use_count,
            p.created_at,
            p.updated_at,
        ])

    return output.getvalue()


def _safe_float(val: Any, default: float = 0.7) -> float:
    """Safely parse a float value with fallback to default."""
    if val is None:
        return default
    val_str = str(val).strip()
    if not val_str:
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        return float(val_str)
    except (ValueError, TypeError, OverflowError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely parse an integer value with fallback to default."""
    if val is None:
        return default
    if isinstance(val, int):
        return val
    val_str = str(val).strip()
    if not val_str:
        return default
    try:
        return int(float(val_str))
    except (ValueError, TypeError, OverflowError):
        return default


def from_csv_string(csv_text: str) -> List[Prompt]:
    """Parse CSV text back into Prompt objects.

    Accepts both current headers (with folder_id/template_id/use_count)
    and legacy headers without them (orphan-prone fields default to
    None/0 via Prompt coercion). Malformed numerics fall back via the
    model's coercers (temperature->0.7, use_count->0).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    prompts: List[Prompt] = []

    for row in reader:
        tags_raw = row.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []
        p = Prompt(
            id=row.get("id") or str(uuid.uuid4()),
            title=row.get("title", "Untitled Prompt"),
            description=row.get("description", ""),
            folder_id=(row.get("folder_id") or None) if "folder_id" in row else None,
            template_id=(row.get("template_id") or None) if "template_id" in row else None,
            template_content=row.get("template_content", ""),
            system_instruction=row.get("system_instruction", ""),
            target_model=row.get("target_model", "General"),
            temperature=_safe_float(row.get("temperature"), default=0.7),
            is_favorite=bool(_safe_int(row.get("is_favorite"), default=0)),
            use_count=_safe_int(row.get("use_count"), default=0) if "use_count" in row else 0,
            tags=tags,
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )
        prompts.append(p)

    return prompts


def format_json_string(obj: Any) -> str:
    """Convert payload dictionary to nicely formatted JSON."""
    return json.dumps(obj, indent=2, ensure_ascii=False)
