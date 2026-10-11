"""Data models for Prompt Manager.

Field contracts: ids are UUID4 strings (model ensures non-empty); timestamps
are ISO-8601 (model normalizes, repository stamps updated_at); folder_id /
template_id / parent_id normalize "" -> None with repository nullifying
orphans (never crash); Prompt.tags holds Tag *names* (canonical: strip,
lstrip("#"), lower, dedupe); temperature clamped 0.0-2.0 (fallback 0.7);
use_count floored at 0; is_favorite uses explicit string handling.
VariableSpec derivation must stay via Prompt/Template.variable_specs().
Round-trip: JSON lossless (except revisions); CSV preserves prompt ids;
Markdown is lossy (no reimport).
"""

from dataclasses import dataclass, field
from datetime import datetime
import math
import re
import uuid
from typing import Any, List, Optional


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_FALSE_STRINGS = {"0", "false", "no", "n", "off", "f", ""}


def _ensure_ts(value: Any) -> str:
    """Normalize to ISO-8601; empty/malformed/datetime/int handled."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                cand = text[:-1] + "+00:00" if text.endswith("Z") else text
                return datetime.fromisoformat(cand).isoformat()
            except ValueError:
                pass
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value).isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    return datetime.now().isoformat()


def normalize_tag_name(name: Any) -> str:
    """Canonical tag form: strip, strip leading '#', lower."""
    if name is None:
        return ""
    return str(name).strip().lstrip("#").strip().lower()


@dataclass
class VariableSpec:
    """Specification for a template variable detected in prompt text.

    - name: required, stripped; engine regex ``[a-zA-Z0-9_-]+`` is the
      validation owner (direct empty names are tolerated here so template
      extraction never crashes, but callers should treat "" as invalid).
    - default_value/is_multiline/options: coerced in __post_init__.
    """

    name: str
    default_value: str = ""
    is_multiline: bool = False
    options: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = str(self.name).strip() if self.name is not None else ""
        if self.default_value is None:
            self.default_value = ""
        else:
            self.default_value = str(self.default_value)
        if isinstance(self.is_multiline, bool):
            pass
        elif isinstance(self.is_multiline, str):
            self.is_multiline = self.is_multiline.strip().lower() in (
                "1", "true", "yes", "y", "on",
            )
        else:
            self.is_multiline = bool(self.is_multiline)
        if self.options is None:
            self.options = []
        elif isinstance(self.options, str):
            self.options = [self.options.strip()] if self.options.strip() else []
        else:
            try:
                items = list(self.options)
            except TypeError:
                items = []
            seen: set = set()
            cleaned: List[str] = []
            for item in items:
                text = str(item).strip() if item is not None else ""
                if text and text not in seen:
                    seen.add(text)
                    cleaned.append(text)
            self.options = cleaned

    @property
    def has_options(self) -> bool:
        return len(self.options) > 0


@dataclass
class Folder:
    """Hierarchical folder for prompt organization.

    - id: UUID4 (regenerated when blank). Owner: model.
    - name: stripped; empty allowed here, UI/repository should reject.
    - parent_id: "" -> None; orphan handling owned by repository (nullify,
      never crash); DB cascades children on parent delete.
    - icon: blank/None -> "folder". sort_order: int-coerced, fallback 0.
    - created_at: ISO-8601 normalized (malformed/empty -> now()).
      Note: Folder has no updated_at (unlike Prompt/Template).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    parent_id: Optional[str] = None
    icon: str = "folder"
    sort_order: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        if self.id is None or not str(self.id).strip():
            self.id = str(uuid.uuid4())
        else:
            self.id = str(self.id).strip()
        self.name = "" if self.name is None else str(self.name).strip()
        if self.parent_id is None:
            pass
        else:
            text = str(self.parent_id).strip()
            self.parent_id = text if text else None
        if self.icon is None or not str(self.icon).strip():
            self.icon = "folder"
        else:
            self.icon = str(self.icon).strip()
        try:
            raw = str(self.sort_order).strip() if isinstance(self.sort_order, str) else self.sort_order
            if isinstance(raw, str) and not raw:
                self.sort_order = 0
            else:
                self.sort_order = int(float(raw))  # type: ignore[arg-type]
        except (ValueError, TypeError, OverflowError):
            self.sort_order = 0
        self.created_at = _ensure_ts(self.created_at)


@dataclass
class Tag:
    """Tag with color coding for multi-dimensional filtering.

    - id: UUID4 (regenerated when blank). Owner: model.
    - name: canonical form strip/lstrip("#")/lower; repository owns
      uniqueness (Prompt.tags stores these names; ids live in prompt_tags).
    - color: #RRGGBB (case-normalized) else fallback #3b82f6.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    color: str = "#3b82f6"

    def __post_init__(self) -> None:
        if self.id is None or not str(self.id).strip():
            self.id = str(uuid.uuid4())
        else:
            self.id = str(self.id).strip()
        self.name = normalize_tag_name(self.name)
        if isinstance(self.color, str) and _HEX_RE.match(self.color.strip()):
            self.color = self.color.strip().lower()
        else:
            self.color = "#3b82f6"

    @staticmethod
    def normalize_name(name: Any) -> str:
        """Canonical tag name shared by Prompt tags and repository."""
        return normalize_tag_name(name)


@dataclass
class PromptRevision:
    """Snapshot revision of a prompt's historical state.

    - id: UUID4 (regenerated when blank). Note: repository._record_revision
      mints ids via SQL; this default covers direct construction.
    - prompt_id: stripped; "" means unset (repository owns FK existence).
    - revision_number: coerced int, floored at 1; repository owns MAX+1.
    - created_at: ISO-8601 normalized; repository SQL stamps UTC-seconds.
      Revisions are never exported (JSON/CSV/Markdown exclude by design).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    prompt_id: str = ""
    revision_number: int = 1
    title: str = ""
    template_content: str = ""
    system_instruction: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        if self.id is None or not str(self.id).strip():
            self.id = str(uuid.uuid4())
        else:
            self.id = str(self.id).strip()
        self.prompt_id = "" if self.prompt_id is None else str(self.prompt_id).strip()
        try:
            raw = str(self.revision_number).strip() if isinstance(self.revision_number, str) else self.revision_number
            if isinstance(raw, str) and not raw:
                self.revision_number = 1
            else:
                self.revision_number = int(float(raw))  # type: ignore[arg-type]
        except (ValueError, TypeError, OverflowError):
            self.revision_number = 1
        if self.revision_number < 1:
            self.revision_number = 1
        self.title = "" if self.title is None else str(self.title)
        self.template_content = "" if self.template_content is None else str(self.template_content)
        self.system_instruction = "" if self.system_instruction is None else str(self.system_instruction)
        self.created_at = _ensure_ts(self.created_at)


@dataclass
class PromptTemplate:
    """Reusable template definition for creating prompts.

    Infrastructure only — no default templates are seeded.
    Users can create arbitrary templates (e.g. 'code-review', 'summarization',
    'chat', 'tool-use') and instantiate Prompts from them via `template_id`.
    - category: normalized strip/lower, blank -> "general" (repository
      owns empty-check for name/content and uniqueness of name).
    - created_at/updated_at: ISO-8601 normalized; repository fills missing
      on insert and overwrites updated_at on update.
    - variable_specs(): extract_variables(content + "\\n" + system) — the
      single canonical derivation; callers must not reimplement.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    content: str = ""
    system_instruction: str = ""
    category: str = "general"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        if self.id is None or not str(self.id).strip():
            self.id = str(uuid.uuid4())
        else:
            self.id = str(self.id).strip()
        self.name = "" if self.name is None else str(self.name).strip()
        self.description = "" if self.description is None else str(self.description)
        self.content = "" if self.content is None else str(self.content)
        self.system_instruction = "" if self.system_instruction is None else str(self.system_instruction)
        if self.category is None or not str(self.category).strip():
            self.category = "general"
        else:
            self.category = str(self.category).strip().lower()
        self.created_at = _ensure_ts(self.created_at)
        self.updated_at = _ensure_ts(self.updated_at)

    def variable_specs(self) -> List["VariableSpec"]:
        """Derive VariableSpecs from content + system_instruction.

        Canonical derivation shared with Prompt.variable_specs(); stays in
        sync with template_engine.extract_variables output.
        """
        from prompt_manager.core.template_engine import extract_variables

        combined = f"{self.content}\n{self.system_instruction}"
        return extract_variables(combined)


@dataclass
class Prompt:
    """Core Prompt entity.

    - id: UUID4 (regenerated when blank), stable across revisions/exports.
    - title: blank/None -> "Untitled Prompt" (UI also applies this).
    - folder_id/template_id: "" -> None; orphans nullified by repository
      (never crash); DB ON DELETE SET NULL (prompts survive parent delete).
    - tags: Tag *names* canonicalized (strip/lstrip("#")/lower/dedupe);
      ids resolved in prompt_tags by repository. In-memory order = insertion;
      DB read order = sorted.
    - temperature: coerced float clamped 0.0-2.0, malformed -> 0.7.
    - is_favorite: explicit "false"/"0"/etc handling (bool("false") trap
      avoided). NOTE: toggle_favorite/increment_use_count bypass the model
      and do not touch updated_at (not treated as content edits).
    - use_count: coerced int floored at 0; repository owns increments.
      NOTE: save_prompt UPDATE preserves stored use_count (in-memory edits
      to use_count are ignored except via increment_use_count).
    - target_model: blank/None -> "General"; free-text allowed (UI combo
      editable). Exporters map "General" -> provider defaults.
    - created_at/updated_at: ISO-8601 normalized; repository fills missing
      on insert, overwrites updated_at on content save.
    - variable_specs(): same derivation as PromptTemplate (content + system).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "Untitled Prompt"
    description: str = ""
    folder_id: Optional[str] = None
    template_content: str = ""
    system_instruction: str = ""
    target_model: str = "General"
    temperature: float = 0.7
    is_favorite: bool = False
    use_count: int = 0
    tags: List[str] = field(default_factory=list)
    # Optional link to a PromptTemplate this prompt was instantiated from
    template_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        if self.id is None or not str(self.id).strip():
            self.id = str(uuid.uuid4())
        else:
            self.id = str(self.id).strip()
        if self.title is None or not str(self.title).strip():
            self.title = "Untitled Prompt"
        else:
            self.title = str(self.title).strip()
        self.description = "" if self.description is None else str(self.description)
        for attr in ("folder_id", "template_id"):
            value = getattr(self, attr)
            if value is None:
                continue
            text = str(value).strip()
            setattr(self, attr, text if text else None)
        self.template_content = "" if self.template_content is None else str(self.template_content)
        self.system_instruction = "" if self.system_instruction is None else str(self.system_instruction)
        if self.target_model is None or not str(self.target_model).strip():
            self.target_model = "General"
        else:
            self.target_model = str(self.target_model).strip()
        self.temperature = self._coerce_temperature(self.temperature)
        self.is_favorite = self._coerce_bool(self.is_favorite)
        self.use_count = self._coerce_use_count(self.use_count)
        self.tags = self._normalize_tags(self.tags)
        self.created_at = _ensure_ts(self.created_at)
        self.updated_at = _ensure_ts(self.updated_at)

    @staticmethod
    def _coerce_temperature(value: Any) -> float:
        if value is None:
            return 0.7
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        try:
            raw = str(value).strip() if isinstance(value, str) else value
            if isinstance(raw, str) and not raw:
                return 0.7
            parsed = float(raw)  # type: ignore[arg-type]
        except (ValueError, TypeError, OverflowError):
            return 0.7
        if math.isnan(parsed) or math.isinf(parsed):
            return 0.7
        return max(0.0, min(2.0, parsed))

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _FALSE_STRINGS:
                return False
            if lowered:
                return True
            return False
        return bool(value)

    @staticmethod
    def _coerce_use_count(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        try:
            raw = str(value).strip() if isinstance(value, str) else value
            if isinstance(raw, str) and not raw:
                return 0
            number = int(float(raw))  # type: ignore[arg-type]
        except (ValueError, TypeError, OverflowError):
            return 0
        return max(0, number)

    @staticmethod
    def _normalize_tags(tags: Any) -> List[str]:
        if tags is None:
            return []
        if isinstance(tags, str):
            tags = [tags]
        try:
            items = list(tags)
        except TypeError:
            return []
        seen: set = set()
        result: List[str] = []
        for item in items:
            clean = normalize_tag_name(item)
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    def variable_specs(self) -> List["VariableSpec"]:
        """Derive VariableSpecs from template_content + system_instruction.

        Mirrors PromptTemplate.variable_specs() so both stay in sync with
        template_engine.extract_variables output.
        """
        from prompt_manager.core.template_engine import extract_variables

        combined = f"{self.template_content}\n{self.system_instruction}"
        return extract_variables(combined)
