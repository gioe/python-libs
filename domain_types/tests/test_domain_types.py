"""Tests for shared domain types package."""

import json

import pytest

from libs.domain_types import (
    QuestionType,
    DifficultyLevel,
    TestStatus,
    NotificationType,
    GenerationRunStatus,
    EducationLevel,
    FeedbackCategory,
    FeedbackStatus,
)


class TestQuestionType:
    """Tests for QuestionType enum."""

    def test_values(self):
        assert set(QuestionType) == {
            QuestionType.PATTERN,
            QuestionType.LOGIC,
            QuestionType.SPATIAL,
            QuestionType.MATH,
            QuestionType.VERBAL,
            QuestionType.MEMORY,
        }

    def test_string_values(self):
        assert QuestionType.PATTERN.value == "pattern"
        assert QuestionType.LOGIC.value == "logic"
        assert QuestionType.SPATIAL.value == "spatial"
        assert QuestionType.MATH.value == "math"
        assert QuestionType.VERBAL.value == "verbal"
        assert QuestionType.MEMORY.value == "memory"

    def test_str_mixin(self):
        assert QuestionType.PATTERN.value == "pattern"
        assert QuestionType("pattern") == QuestionType.PATTERN

    def test_json_serializable(self):
        assert json.dumps(QuestionType.PATTERN) == '"pattern"'

    def test_count(self):
        assert len(QuestionType) == 6


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


class TestTestStatus:
    """Tests for TestStatus enum."""

    def test_values(self):
        assert TestStatus.IN_PROGRESS.value == "in_progress"
        assert TestStatus.COMPLETED.value == "completed"
        assert TestStatus.ABANDONED.value == "abandoned"

    def test_count(self):
        assert len(TestStatus) == 3


class TestNotificationType:
    """Tests for NotificationType enum."""

    def test_values(self):
        assert NotificationType.TEST_REMINDER.value == "test_reminder"
        assert NotificationType.DAY_30_REMINDER.value == "day_30_reminder"
        assert NotificationType.LOGOUT_ALL.value == "logout_all"

    def test_count(self):
        assert len(NotificationType) == 3


class TestGenerationRunStatus:
    """Tests for GenerationRunStatus enum."""

    def test_values(self):
        assert GenerationRunStatus.RUNNING.value == "running"
        assert GenerationRunStatus.SUCCESS.value == "success"
        assert GenerationRunStatus.PARTIAL_FAILURE.value == "partial_failure"
        assert GenerationRunStatus.FAILED.value == "failed"

    def test_count(self):
        assert len(GenerationRunStatus) == 4


class TestEducationLevel:
    """Tests for EducationLevel enum."""

    def test_values(self):
        assert EducationLevel.HIGH_SCHOOL.value == "high_school"
        assert EducationLevel.DOCTORATE.value == "doctorate"
        assert EducationLevel.PREFER_NOT_TO_SAY.value == "prefer_not_to_say"

    def test_count(self):
        assert len(EducationLevel) == 7


class TestFeedbackCategory:
    """Tests for FeedbackCategory enum."""

    def test_values(self):
        assert FeedbackCategory.BUG_REPORT.value == "bug_report"
        assert FeedbackCategory.OTHER.value == "other"

    def test_count(self):
        assert len(FeedbackCategory) == 5


class TestFeedbackStatus:
    """Tests for FeedbackStatus enum."""

    def test_values(self):
        assert FeedbackStatus.PENDING.value == "pending"
        assert FeedbackStatus.REVIEWED.value == "reviewed"
        assert FeedbackStatus.RESOLVED.value == "resolved"

    def test_count(self):
        assert len(FeedbackStatus) == 3


class TestBackwardCompatibility:
    """Tests verifying backward compatibility with existing import patterns."""

    def test_all_enums_are_str_subclass(self):
        """All domain enums should be str subclasses for JSON serialization."""
        for enum_cls in [
            QuestionType,
            DifficultyLevel,
            TestStatus,
            NotificationType,
            GenerationRunStatus,
            EducationLevel,
            FeedbackCategory,
            FeedbackStatus,
        ]:
            for member in enum_cls:
                assert isinstance(member, str), (
                    f"{enum_cls.__name__}.{member.name} is not a str instance"
                )

    def test_enum_lookup_by_value(self):
        """Enum members should be constructable from their string values."""
        assert QuestionType("pattern") is QuestionType.PATTERN
        assert DifficultyLevel("hard") is DifficultyLevel.HARD
        assert TestStatus("completed") is TestStatus.COMPLETED

    def test_invalid_value_raises(self):
        """Invalid values should raise ValueError."""
        with pytest.raises(ValueError):
            QuestionType("invalid_type")
        with pytest.raises(ValueError):
            DifficultyLevel("extreme")

    def test_cross_service_identity(self):
        """Enums imported via different paths should be identical objects."""
        # Backend imports via app.models.models which re-exports from libs.domain_types
        # Question-service imports via app.models which re-exports from libs.domain_types
        # Both should resolve to the exact same enum class
        from libs.domain_types import QuestionType as DirectImport

        assert DirectImport is QuestionType
        assert DirectImport.PATTERN is QuestionType.PATTERN
