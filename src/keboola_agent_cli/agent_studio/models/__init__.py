"""Pydantic models for Agent Studio.

Phase 1 exports only the Playbook shape; the rest of `docs/agents-v2.md`
§ 7 (Tool, Skill, Connection, Plugin, Solution, ApprovalRequest,
BudgetPolicy) arrives as later slices register their own entities.
"""

from __future__ import annotations

from .playbook import Playbook, PlaybookSummary

__all__ = ["Playbook", "PlaybookSummary"]
