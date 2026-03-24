"""Shared domain types for Python microservices.

This package provides generic, reusable domain primitives applicable across
unrelated projects. It is not a home for application-specific enums.

Usage:
    from domain_types import DifficultyLevel, AsyncRunStatus
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


class AsyncRunStatus(str, enum.Enum):
    """Status for any async job or processing run."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class FeedbackStatus(str, enum.Enum):
    """Processing status for a feedback submission."""

    PENDING = "pending"
    REVIEWED = "reviewed"
    RESOLVED = "resolved"


__all__ = [
    "DifficultyLevel",
    "SessionStatus",
    "AsyncRunStatus",
    "FeedbackStatus",
]
