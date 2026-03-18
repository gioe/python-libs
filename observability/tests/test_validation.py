"""Tests for observability validation utilities."""

import pytest

from libs.observability.validation import (
    MAX_TAG_LENGTH,
    validate_json_serializable,
    validate_tag,
)


class TestValidateTag:
    """Tests for validate_tag function."""

    def test_valid_tag(self) -> None:
        """Test validation passes for valid tag key and value."""
        # Should not raise
        validate_tag("my_key", "my_value")

    def test_valid_tag_at_max_length(self) -> None:
        """Test validation passes for tags at maximum length."""
        max_length_str = "a" * MAX_TAG_LENGTH
        validate_tag(max_length_str, max_length_str)

    def test_key_not_string(self) -> None:
        """Test validation fails when key is not a string."""
        with pytest.raises(ValueError, match="Tag key must be a string"):
            validate_tag(123, "value")  # type: ignore[arg-type]

    def test_value_not_string(self) -> None:
        """Test validation fails when value is not a string."""
        with pytest.raises(ValueError, match="Tag value must be a string"):
            validate_tag("key", 123)  # type: ignore[arg-type]

    def test_key_exceeds_max_length(self) -> None:
        """Test validation fails when key exceeds maximum length."""
        too_long_key = "a" * (MAX_TAG_LENGTH + 1)
        with pytest.raises(
            ValueError, match=f"Tag key exceeds maximum length of {MAX_TAG_LENGTH}"
        ):
            validate_tag(too_long_key, "value")

    def test_value_exceeds_max_length(self) -> None:
        """Test validation fails when value exceeds maximum length."""
        too_long_value = "a" * (MAX_TAG_LENGTH + 1)
        with pytest.raises(
            ValueError, match=f"Tag value exceeds maximum length of {MAX_TAG_LENGTH}"
        ):
            validate_tag("key", too_long_value)

    def test_empty_strings_allowed(self) -> None:
        """Test validation passes for empty strings."""
        validate_tag("", "")

    def test_special_characters_allowed(self) -> None:
        """Test validation passes for special characters."""
        validate_tag("key-with_special.chars", "value with spaces & symbols!")

    def test_key_none_fails(self) -> None:
        """Test validation fails when key is None."""
        with pytest.raises(ValueError, match="Tag key must be a string"):
            validate_tag(None, "value")  # type: ignore[arg-type]

    def test_value_none_fails(self) -> None:
        """Test validation fails when value is None."""
        with pytest.raises(ValueError, match="Tag value must be a string"):
            validate_tag("key", None)  # type: ignore[arg-type]

    def test_error_message_includes_key_details(self) -> None:
        """Test error message includes the problematic key."""
        too_long_key = "a" * (MAX_TAG_LENGTH + 1)
        with pytest.raises(ValueError) as exc_info:
            validate_tag(too_long_key, "value")

        error_msg = str(exc_info.value)
        assert "Tag key exceeds maximum length" in error_msg
        assert str(MAX_TAG_LENGTH + 1) in error_msg

    def test_error_message_includes_value_details(self) -> None:
        """Test error message includes the problematic value."""
        too_long_value = "a" * (MAX_TAG_LENGTH + 1)
        with pytest.raises(ValueError) as exc_info:
            validate_tag("key", too_long_value)

        error_msg = str(exc_info.value)
        assert "Tag value exceeds maximum length" in error_msg
        assert str(MAX_TAG_LENGTH + 1) in error_msg


class TestValidateJsonSerializable:
    """Tests for validate_json_serializable function."""

    def test_valid_data(self) -> None:
        """Test validation passes for JSON-serializable data."""
        data = {
            "string": "value",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "list": [1, 2, 3],
            "dict": {"nested": "value"},
        }
        validate_json_serializable(data)

    def test_empty_dict(self) -> None:
        """Test validation passes for empty dict."""
        validate_json_serializable({})

    def test_nested_structures(self) -> None:
        """Test validation passes for nested structures."""
        data = {
            "level1": {
                "level2": {
                    "level3": ["a", "b", "c"],
                },
            },
        }
        validate_json_serializable(data)

    def test_not_dict_fails(self) -> None:
        """Test validation fails when data is not a dict."""
        with pytest.raises(ValueError, match="Data must be a dictionary"):
            validate_json_serializable("not a dict")  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Data must be a dictionary"):
            validate_json_serializable([1, 2, 3])  # type: ignore[arg-type]

    def test_non_serializable_object(self) -> None:
        """Test validation fails for non-serializable objects."""

        class CustomClass:
            pass

        data = {"custom": CustomClass()}
        with pytest.raises(ValueError, match="non-JSON-serializable value"):
            validate_json_serializable(data)

    def test_error_identifies_problematic_key(self) -> None:
        """Test error message identifies which key has non-serializable value."""

        class CustomClass:
            pass

        data = {
            "good_key": "good_value",
            "bad_key": CustomClass(),
            "another_good": 123,
        }
        with pytest.raises(ValueError) as exc_info:
            validate_json_serializable(data)

        error_msg = str(exc_info.value)
        assert "bad_key" in error_msg
        assert "CustomClass" in error_msg

    def test_function_not_serializable(self) -> None:
        """Test validation fails for functions."""

        def my_func() -> None:
            pass

        data = {"func": my_func}
        with pytest.raises(ValueError, match="non-JSON-serializable"):
            validate_json_serializable(data)

    def test_set_not_serializable(self) -> None:
        """Test validation fails for sets."""
        data = {"my_set": {1, 2, 3}}
        with pytest.raises(ValueError, match="non-JSON-serializable"):
            validate_json_serializable(data)

    def test_bytes_not_serializable(self) -> None:
        """Test validation fails for bytes."""
        data = {"bytes": b"hello"}
        with pytest.raises(ValueError, match="non-JSON-serializable"):
            validate_json_serializable(data)

    def test_circular_reference_not_serializable(self) -> None:
        """Test validation fails for circular references."""
        data = {"key": "value"}
        data["self"] = data  # type: ignore[assignment]
        with pytest.raises(ValueError, match="non-JSON-serializable"):
            validate_json_serializable(data)
