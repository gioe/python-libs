"""Validation utilities for observability inputs."""

import json
from typing import Any


# Sentry tag length limits
MAX_TAG_LENGTH = 200


def validate_tag(key: str, value: str) -> None:
    """Validate a tag key and value meet Sentry requirements.

    Args:
        key: Tag key to validate.
        value: Tag value to validate.

    Raises:
        ValueError: If key or value fail validation.

    Requirements:
        - Both key and value must be strings
        - Key length must be <= 200 characters
        - Value length must be <= 200 characters
    """
    if not isinstance(key, str):
        raise ValueError(
            f"Tag key must be a string, got {type(key).__name__}: {key!r}"
        )

    if not isinstance(value, str):
        raise ValueError(
            f"Tag value must be a string, got {type(value).__name__}: {value!r}"
        )

    if len(key) > MAX_TAG_LENGTH:
        raise ValueError(
            f"Tag key exceeds maximum length of {MAX_TAG_LENGTH} characters: "
            f"'{key}' ({len(key)} characters)"
        )

    if len(value) > MAX_TAG_LENGTH:
        raise ValueError(
            f"Tag value exceeds maximum length of {MAX_TAG_LENGTH} characters: "
            f"'{value}' ({len(value)} characters)"
        )


def validate_json_serializable(data: dict[str, Any]) -> None:
    """Validate that data is JSON-serializable.

    Args:
        data: Dictionary to validate.

    Raises:
        ValueError: If data contains non-serializable values.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Data must be a dictionary, got {type(data).__name__}")

    try:
        json.dumps(data)
    except (TypeError, ValueError) as e:
        # Try to identify which key/value caused the issue
        for key, value in data.items():
            try:
                json.dumps({key: value})
            except (TypeError, ValueError):
                raise ValueError(
                    f"Data contains non-JSON-serializable value for key '{key}': "
                    f"{type(value).__name__}"
                ) from e
        # If we can't identify the specific key, raise the original error
        raise ValueError(f"Data is not JSON-serializable: {e}") from e
