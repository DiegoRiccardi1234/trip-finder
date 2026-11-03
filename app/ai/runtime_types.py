"""Small response contract shared by native and API connection adapters."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_JSON_ENVELOPE = {
    "type": "object",
    "properties": {"json": {"type": "string"}},
    "required": ["json"],
    "additionalProperties": False,
}


def normalize_runtime_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Choose a strict wire schema without narrowing the application contract.

    Native Structured Outputs requires all properties and closed objects. A
    Pydantic schema with defaults, optional properties or arbitrary maps cannot
    be made strict by simply closing it: doing so loses valid values. Such a
    schema travels as one serialized JSON string and remains validated against
    the original schema locally, with no second inference or repair attempt.

    Arrays of objects use the same framing as a runtime compatibility policy:
    the observed native host completed the same analysis through that framing
    while direct structured generation did not return a completion. This does
    not claim that those schemas are invalid or identify the host's failure.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Schema AI non valido.") from None

    def incompatible(value: Any, *, root: bool = False) -> bool:
        if not isinstance(value, dict):
            return True
        if "default" in value or any(
            key in value
            for key in (
                "allOf",
                "oneOf",
                "not",
                "if",
                "then",
                "else",
                "dependentRequired",
                "dependentSchemas",
                "patternProperties",
                "unevaluatedProperties",
                "uniqueItems",
            )
        ):
            return True
        if root and (value.get("type") != "object" or "anyOf" in value):
            return True
        kind = value.get("type")
        is_object = kind == "object" or (isinstance(kind, list) and "object" in kind)
        if is_object or "properties" in value or "additionalProperties" in value:
            properties = value.get("properties", {})
            if value.get("additionalProperties") is not False or not isinstance(properties, dict):
                return True
            if set(value.get("required", [])) != set(properties):
                return True
            if any(incompatible(child) for child in properties.values()):
                return True
        if "items" in value:
            item = value["items"]
            if isinstance(item, dict) and (
                item.get("type") == "object" or "properties" in item or "$ref" in item
            ):
                return True
            if incompatible(item):
                return True
        if any(incompatible(child) for child in value.get("anyOf", [])):
            return True
        if any(incompatible(child) for child in value.get("$defs", {}).values()):
            return True
        return False

    wrapped = incompatible(schema, root=True)
    return deepcopy(_JSON_ENVELOPE if wrapped else schema), wrapped


class TruncatedCompletionError(ValueError):
    """A declared length stop is invalid even when its outer JSON parses."""

    def __init__(self, model: str = "") -> None:
        self.model = model
        super().__init__("Risposta AI troncata; nessun risultato salvato.")


def validate_output(value: Any, schema: dict[str, Any]) -> None:
    """Validate locally without including data or provider messages in errors."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, ValidationError

    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(value)
    except (SchemaError, ValidationError):
        raise ValueError("Risposta AI non conforme allo schema richiesto.") from None
