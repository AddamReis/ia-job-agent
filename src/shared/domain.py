"""Modelos de domínio compartilhados — reexportação sem duplicar fontes de verdade."""
from __future__ import annotations

from ..core.state import ExecutionStatus, JobStatus, can_transition
from ..models.domain import (
    AnswerRecord,
    Application,
    ApplicationDependency,
    ApprovalRequest,
    CandidateProfile,
    Company,
    DailyLimit,
    ExecutionHistory,
    JobAnalysis,
    JobOpportunity,
    QualificationFeedback,
    QualificationResult,
)

__all__ = [
    "JobStatus",
    "ExecutionStatus",
    "can_transition",
    "CandidateProfile",
    "JobOpportunity",
    "JobAnalysis",
    "QualificationResult",
    "Company",
    "Application",
    "ApplicationDependency",
    "AnswerRecord",
    "ApprovalRequest",
    "ExecutionHistory",
    "DailyLimit",
    "QualificationFeedback",
]
