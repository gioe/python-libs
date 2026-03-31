"""Pure string manipulation utilities with no domain dependencies."""

import re
from typing import Any, List, Optional


class StringUtils:
    """Pure string manipulation utilities."""

    @staticmethod
    def remove_non_alphanumeric(string: str) -> str:
        """Remove all non-alphanumeric characters from a string."""
        return "".join(ch for ch in string if ch.isalnum())

    @staticmethod
    def is_valid_zip_code(zip_code: str) -> bool:
        """
        Validate US ZIP code format.

        Args:
            zip_code: ZIP code to validate

        Returns:
            bool: True if ZIP code format is valid
        """
        pattern = r"^\d{5}(-\d{4})?$"
        return bool(re.match(pattern, zip_code))

    @staticmethod
    def is_valid_url(url: str) -> bool:
        """
        Validate URL format.

        Args:
            url: URL to validate

        Returns:
            bool: True if URL format is valid
        """
        if not url:
            return False

        pattern = r"^https?://(?:[-\w.])+(?:\:[0-9]+)?(?:/(?:[\w/_.])*(?:\?(?:[\w&=%.])*)?(?:\#(?:[\w.])*)?)?$"
        return re.match(pattern, url) is not None

    @staticmethod
    def is_valid_phone(phone: str) -> bool:
        """
        Validate phone number format.

        Args:
            phone: Phone number to validate

        Returns:
            bool: True if phone format is valid
        """
        if not phone:
            return False

        digits = re.sub(r"\D", "", phone)
        return 10 <= len(digits) <= 15

    @staticmethod
    def is_valid_email(email: str) -> bool:
        """
        Validate email address format.

        Args:
            email: Email address to validate

        Returns:
            bool: True if email format is valid
        """
        if not email:
            return False

        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return re.match(pattern, email) is not None

    @staticmethod
    def remove_parentheses_content(text: str) -> str:
        """Remove content within parentheses from text."""
        result = ""
        inside_parentheses = False

        for char in text:
            if char == "(":
                inside_parentheses = True
            elif char == ")":
                inside_parentheses = False
            elif not inside_parentheses:
                result += char

        return result.strip()

    @staticmethod
    def normalize_whitespace(text: str) -> str:
        """Normalize whitespace in text by removing extra spaces and newlines."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text.strip())

    @staticmethod
    def remove_prefix(text: str, prefix: str, case_sensitive: bool = True) -> str:
        """Remove prefix from text if it exists."""
        if not text or not prefix:
            return text

        if case_sensitive:
            if text.startswith(prefix):
                return text[len(prefix):]
        else:
            if text.lower().startswith(prefix.lower()):
                return text[len(prefix):]

        return text

    @staticmethod
    def remove_suffix(text: str, suffix: str, case_sensitive: bool = True) -> str:
        """Remove suffix from text if it exists."""
        if not text or not suffix:
            return text

        if case_sensitive:
            if text.endswith(suffix):
                return text[:-len(suffix)]
        else:
            if text.lower().endswith(suffix.lower()):
                return text[:-len(suffix)]

        return text

    @staticmethod
    def title_case_smart(text: str, ignore_words: Optional[List[str]] = None) -> str:
        """
        Smart title case that keeps certain words lowercase.

        Args:
            text: Text to convert to title case
            ignore_words: List of words to keep lowercase (articles, prepositions, etc.)

        Returns:
            Title cased text with specified words kept lowercase
        """
        if not text:
            return ""

        if ignore_words is None:
            ignore_words = ["the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"]

        words = text.split()
        result = []

        for i, word in enumerate(words):
            if i == 0 or i == len(words) - 1:
                result.append(word.capitalize())
            elif word.lower() in ignore_words:
                result.append(word.lower())
            else:
                result.append(word.capitalize())

        return " ".join(result)

    @staticmethod
    def extract_between(text: str, start_marker: str, end_marker: str, include_markers: bool = False) -> Optional[str]:
        """
        Extract text between two markers.

        Args:
            text: Source text
            start_marker: Starting marker
            end_marker: Ending marker
            include_markers: Whether to include the markers in the result

        Returns:
            Extracted text or None if markers not found
        """
        if not text or not start_marker or not end_marker:
            return None

        start_pos = text.find(start_marker)
        if start_pos == -1:
            return None

        search_start = start_pos + len(start_marker)
        end_pos = text.find(end_marker, search_start)
        if end_pos == -1:
            return None

        if include_markers:
            return text[start_pos:end_pos + len(end_marker)]
        else:
            return text[search_start:end_pos]

    @staticmethod
    def slugify(text: str, separator: str = "-") -> str:
        """
        Convert text to a URL-friendly slug.

        Args:
            text: Text to slugify
            separator: Character to use as separator (default: "-")

        Returns:
            Slugified text
        """
        if not text:
            return ""

        text = text.lower()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_-]+", separator, text)
        return text.strip(separator)

    @staticmethod
    def truncate(text: str, max_length: int, suffix: str = "...") -> str:
        """
        Truncate text to specified length, adding suffix if truncated.

        Args:
            text: Text to truncate
            max_length: Maximum length of result (including suffix)
            suffix: Suffix to add if text is truncated

        Returns:
            Truncated text
        """
        if not text or len(text) <= max_length:
            return text

        if len(suffix) >= max_length:
            return text[:max_length]

        return text[:max_length - len(suffix)] + suffix

    @staticmethod
    def clean_string_field(value: Any) -> str:
        """
        Clean and normalize string fields.

        Args:
            value: Value to clean

        Returns:
            Cleaned string value
        """
        if value is None:
            return ""

        cleaned = str(value).strip()
        cleaned = " ".join(cleaned.split())
        return cleaned

    @staticmethod
    def extract_price(text: str) -> str:
        """
        Extract price from text string.

        Args:
            text: Text containing price information

        Returns:
            Extracted price as string, or '0' if no price found
        """
        pattern = r"\d+(\.\d{2})?"
        match = re.search(pattern, text)
        return match.group() if match else "0"

    @staticmethod
    def clean_html_content(content: str) -> Optional[str]:
        """
        Remove HTML comments and excess whitespace from content.

        Args:
            content: Raw content that may contain HTML comments

        Returns:
            Optional[str]: Cleaned content or None if empty after cleaning
        """
        if not content:
            return None

        cleaned = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
        cleaned = cleaned.strip()
        return cleaned if cleaned else None
