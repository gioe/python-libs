"""Shared domain types for Python microservices.

This package provides generic, reusable domain primitives applicable across
unrelated projects. It is not a home for application-specific enums.

Usage:
    from domain_types import DifficultyLevel, SessionStatus
"""

import enum


class DifficultyLevel(str, enum.Enum):
    """Difficulty levels applicable to any gradable content."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class SessionStatus(str, enum.Enum):
    """Generic status for a session or workflow run."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class GenerationRunStatus(str, enum.Enum):
    """Status for an async generation or processing run."""

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
    """Category for user-submitted feedback."""

    BUG_REPORT = "bug_report"
    FEATURE_REQUEST = "feature_request"
    GENERAL_FEEDBACK = "general_feedback"
    QUESTION_HELP = "question_help"
    OTHER = "other"


class FeedbackStatus(str, enum.Enum):
    """Processing status for a feedback submission."""

    PENDING = "pending"
    REVIEWED = "reviewed"
    RESOLVED = "resolved"


__all__ = [
    "DifficultyLevel",
    "SessionStatus",
    "GenerationRunStatus",
    "EducationLevel",
    "FeedbackCategory",
    "FeedbackStatus",
]
