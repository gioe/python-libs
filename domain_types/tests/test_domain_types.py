"""Tests for shared domain types package."""

import json

import pytest

from domain_types import (
    DifficultyLevel,
    SessionStatus,
    AsyncRunStatus,
    FeedbackStatus,
)


class TestDifficultyLevel:
    """Tests for DifficultyLevel enum."""

    def test_values(self):
        assert DifficultyLevel.EASY.value == "easy"
        assert DifficultyLevel.MEDIUM.value == "medium"
        assert DifficultyLevel.HARD.value == "hard"

    def test_count(self):
        assert len(DifficultyLevel) == 3

    def test_str_mixin(self):
        assert DifficultyLevel("easy") == DifficultyLevel.EASY

    def test_json_serializable(self):
        assert json.dumps(DifficultyLevel.EASY) == '"easy"'


class TestSessionStatus:
    """Tests for SessionStatus enum."""

    def test_values(self):
        assert SessionStatus.IN_PROGRESS.value == "in_progress"
        assert SessionStatus.COMPLETED.value == "completed"
        assert SessionStatus.ABANDONED.value == "abandoned"

    def test_count(self):
        assert len(SessionStatus) == 3

    def test_str_mixin(self):
        assert SessionStatus("completed") == SessionStatus.COMPLETED


class TestAsyncRunStatus:
    """Tests for AsyncRunStatus enum."""

    def test_values(self):
        assert AsyncRunStatus.RUNNING.value == "running"
        assert AsyncRunStatus.SUCCESS.value == "success"
        assert AsyncRunStatus.PARTIAL_FAILURE.value == "partial_failure"
        assert AsyncRunStatus.FAILED.value == "failed"

    def test_count(self):
        assert len(AsyncRunStatus) == 4


class TestFeedbackStatus:
    """Tests for FeedbackStatus enum."""

    def test_values(self):
        assert FeedbackStatus.PENDING.value == "pending"
        assert FeedbackStatus.REVIEWED.value == "reviewed"
        assert FeedbackStatus.RESOLVED.value == "resolved"

    def test_count(self):
        assert len(FeedbackStatus) == 3


class TestAllEnumsAreStrSubclass:
    """All retained enums should be str subclasses for JSON serialization."""

    def test_str_mixin(self):
        for enum_cls in [
            DifficultyLevel,
            SessionStatus,
            AsyncRunStatus,
            FeedbackStatus,
        ]:
            for member in enum_cls:
                assert isinstance(member, str), (
                    f"{enum_cls.__name__}.{member.name} is not a str instance"
                )

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            DifficultyLevel("extreme")
        with pytest.raises(ValueError):
            SessionStatus("invalid_status")
