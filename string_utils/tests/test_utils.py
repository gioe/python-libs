"""Tests for StringUtils."""

import pytest
from string_utils.utils import StringUtils


class TestSlugify:
    def test_basic(self):
        assert StringUtils.slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert StringUtils.slugify("Hello, World!") == "hello-world"

    def test_custom_separator(self):
        assert StringUtils.slugify("Hello World", separator="_") == "hello_world"

    def test_empty(self):
        assert StringUtils.slugify("") == ""

    def test_multiple_spaces(self):
        assert StringUtils.slugify("hello   world") == "hello-world"

    def test_leading_trailing_separators(self):
        assert StringUtils.slugify("-hello-") == "hello"


class TestIsValidEmail:
    def test_valid(self):
        assert StringUtils.is_valid_email("user@example.com") is True

    def test_valid_with_dots(self):
        assert StringUtils.is_valid_email("user.name+tag@sub.example.org") is True

    def test_invalid_no_at(self):
        assert StringUtils.is_valid_email("notanemail") is False

    def test_invalid_no_domain(self):
        assert StringUtils.is_valid_email("user@") is False

    def test_empty(self):
        assert StringUtils.is_valid_email("") is False

    def test_none_equivalent(self):
        assert StringUtils.is_valid_email("") is False


class TestNormalizeWhitespace:
    def test_multiple_spaces(self):
        assert StringUtils.normalize_whitespace("hello   world") == "hello world"

    def test_newlines(self):
        assert StringUtils.normalize_whitespace("hello\nworld") == "hello world"

    def test_tabs(self):
        assert StringUtils.normalize_whitespace("hello\tworld") == "hello world"

    def test_leading_trailing(self):
        assert StringUtils.normalize_whitespace("  hello  ") == "hello"

    def test_empty(self):
        assert StringUtils.normalize_whitespace("") == ""


class TestExtractBetween:
    def test_basic(self):
        assert StringUtils.extract_between("hello [world] foo", "[", "]") == "world"

    def test_include_markers(self):
        assert StringUtils.extract_between("hello [world] foo", "[", "]", include_markers=True) == "[world]"

    def test_not_found(self):
        assert StringUtils.extract_between("hello world", "[", "]") is None

    def test_empty_text(self):
        assert StringUtils.extract_between("", "[", "]") is None

    def test_missing_end_marker(self):
        assert StringUtils.extract_between("hello [world", "[", "]") is None


class TestTruncate:
    def test_no_truncation_needed(self):
        assert StringUtils.truncate("hello", 10) == "hello"

    def test_truncation(self):
        assert StringUtils.truncate("hello world", 8) == "hello..."

    def test_exact_length(self):
        assert StringUtils.truncate("hello", 5) == "hello"

    def test_custom_suffix(self):
        assert StringUtils.truncate("hello world", 7, suffix="…") == "hello …"

    def test_empty(self):
        assert StringUtils.truncate("", 10) == ""

    def test_suffix_longer_than_max(self):
        assert StringUtils.truncate("hello", 2, suffix="...") == "he"
