"""Shared domain types for AIQ services.

This package is the single source of truth for domain enums used across
the backend, question-service, and (indirectly via OpenAPI) the iOS app.

Usage:
    from libs.domain_types import QuestionType, DifficultyLevel
"""

import enum


class QuestionType(str, enum.Enum):
    """Types of IQ test questions."""

    PATTERN = "pattern"
    LOGIC = "logic"
    SPATIAL = "spatial"
    MATH = "math"
    VERBAL = "verbal"
    MEMORY = "memory"


class DifficultyLevel(str, enum.Enum):
    """Difficulty levels for questions."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TestStatus(str, enum.Enum):
    """Test session status."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class NotificationType(str, enum.Enum):
    """Notification type for APNs push notifications."""

    TEST_REMINDER = "test_reminder"
    DAY_30_REMINDER = "day_30_reminder"
    LOGOUT_ALL = "logout_all"


class GenerationRunStatus(str, enum.Enum):
    """Status for question generation runs."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class EducationLevel(str, enum.Enum):
    """Education level for demographic data."""

    HIGH_SCHOOL = "high_school"
    SOME_COLLEGE = "some_college"
    ASSOCIATES = "associates"
    BACHELORS = "bachelors"
    MASTERS = "masters"
    DOCTORATE = "doctorate"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class FeedbackCategory(str, enum.Enum):
    """Feedback category."""

    BUG_REPORT = "bug_report"
    FEATURE_REQUEST = "feature_request"
    GENERAL_FEEDBACK = "general_feedback"
    QUESTION_HELP = "question_help"
    OTHER = "other"


class FeedbackStatus(str, enum.Enum):
    """Feedback submission status."""

    PENDING = "pending"
    REVIEWED = "reviewed"
    RESOLVED = "resolved"


__all__ = [
    "QuestionType",
    "DifficultyLevel",
    "TestStatus",
    "NotificationType",
    "GenerationRunStatus",
    "EducationLevel",
    "FeedbackCategory",
    "FeedbackStatus",
]
