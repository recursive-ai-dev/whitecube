"""Prompt variable templating, token extraction, and hydration engine."""

import re
from typing import Dict, List, Set, Tuple
from prompt_manager.core.models import VariableSpec

# Matches: {{name}}, {{name:default}}, {{name|modifier}}, {{name:default|modifier}}
# e.g., {{language:Python}}, {{code|multiline}}, {{tone:friendly|options:casual,friendly,formal}}
VARIABLE_PATTERN = re.compile(
    r"\{\{\s*([a-zA-Z0-9_\-]+)\s*(?::\s*([^}|]+?)\s*)?(?:\|\s*([^}]+?)\s*)?\}\}"
)


def extract_variables(template: str) -> List[VariableSpec]:
    """Extract unique variable specifications from a template string in order of appearance."""
    if not template:
        return []

    specs_by_name: Dict[str, VariableSpec] = {}
    ordered_names: List[str] = []

    for match in VARIABLE_PATTERN.finditer(template):
        name = match.group(1).strip()
        default_val = (match.group(2) or "").strip()
        modifier = (match.group(3) or "").strip()

        is_multiline = False
        options: List[str] = []

        if modifier:
            parts = [p.strip() for p in modifier.split(";")]
            for part in parts:
                if part.lower() == "multiline":
                    is_multiline = True
                elif part.lower().startswith("options:"):
                    raw_opts = part[len("options:"):].split(",")
                    options = [opt.strip() for opt in raw_opts if opt.strip()]

        if name not in specs_by_name:
            specs_by_name[name] = VariableSpec(
                name=name,
                default_value=default_val,
                is_multiline=is_multiline,
                options=options,
            )
            ordered_names.append(name)
        else:
            # Merge / upgrade metadata if later occurrence has more specifics
            existing = specs_by_name[name]
            if default_val and not existing.default_value:
                existing.default_value = default_val
            if is_multiline:
                existing.is_multiline = True
            if options and not existing.options:
                existing.options = options

    return [specs_by_name[name] for name in ordered_names]


def hydrate_template(
    template: str,
    values: Dict[str, str],
    fallback_to_defaults: bool = True,
    preserve_unfilled: bool = False,
) -> str:
    """Substitute variables in the template with supplied user values.

    Args:
        template: The raw prompt template string containing {{variables}}.
        values: Dictionary mapping variable names to their values.
        fallback_to_defaults: If true and variable has a default, use it if value is empty.
        preserve_unfilled: If true, keep {{var}} literal if no value or default is available.
    """
    if not template:
        return ""

    def replace_match(match: re.Match) -> str:
        name = match.group(1).strip()
        default_val = (match.group(2) or "").strip()

        user_val = values.get(name)
        if user_val is not None and str(user_val).strip() != "":
            return str(user_val)

        if fallback_to_defaults and default_val:
            return default_val

        if preserve_unfilled:
            return match.group(0)

        return ""

    return VARIABLE_PATTERN.sub(replace_match, template)


def get_unfilled_variables(template: str, values: Dict[str, str]) -> List[str]:
    """Return list of variable names in template that have neither a user value nor a default."""
    specs = extract_variables(template)
    unfilled = []
    for spec in specs:
        val = values.get(spec.name, "").strip()
        if not val and not spec.default_value:
            unfilled.append(spec.name)
    return unfilled


def check_syntax_errors(template: str) -> List[str]:
    """Check for malformed braces or syntax errors in the prompt template."""
    errors = []
    tokens = []
    for m in re.finditer(r"\{\{|\}\}", template):
        tokens.append((m.start(), m.group(0)))

    depth = 0
    nested_reported = False
    closing_first_reported = False

    for pos, token in tokens:
        if token == "{{":
            depth += 1
            if depth > 1 and not nested_reported:
                errors.append(f"Nested '{{{{' detected without closing braces at position {pos}.")
                nested_reported = True
        elif token == "}}":
            depth -= 1
            if depth < 0:
                if not closing_first_reported:
                    errors.append(f"Unmatched closing '}}}}' found before opening '{{{{' at position {pos}.")
                    closing_first_reported = True
                depth = 0

    openings_count = sum(1 for _, t in tokens if t == "{{")
    closings_count = sum(1 for _, t in tokens if t == "}}")

    if openings_count != closings_count:
        errors.append(
            f"Unmatched braces: found {openings_count} '{{{{' and {closings_count} '}}}}'."
        )
    elif depth > 0:
        errors.append("Unmatched opening '{{' detected.")

    return errors
