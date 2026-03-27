"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# --- Session ---

class SessionCreate(BaseModel):
    developer: str
    agent: str = "unknown"
    scope: list[str] = Field(default_factory=list)
    description: str = ""
    branch: str = ""
    auto_lock: bool = True  # auto-create scope locks from scope patterns
    lock_mode: str = "advisory"  # advisory (warn) or exclusive (block)


class SessionUpdate(BaseModel):
    status: str | None = None  # active|paused|completed
    summary: str | None = None
    scope: list[str] | None = None
    description: str | None = None


class SessionResponse(BaseModel):
    id: str
    developer: str
    agent: str
    scope: list[str]
    description: str
    status: str
    branch: str
    started_at: datetime
    completed_at: datetime | None = None
    summary: str | None = None
    lock_count: int = 0
    decision_count: int = 0
    commit_count: int = 0

    model_config = {"from_attributes": True}


# --- Lock ---

class LockCreate(BaseModel):
    session_id: str
    pattern: str
    mode: str = "advisory"


class LockCheckRequest(BaseModel):
    paths: list[str]


class LockCheckResult(BaseModel):
    path: str
    locked: bool
    lock_id: str | None = None
    session_id: str | None = None
    developer: str | None = None
    mode: str | None = None
    pattern: str | None = None


class LockResponse(BaseModel):
    id: str
    session_id: str
    pattern: str
    mode: str
    created_at: datetime
    expires_at: datetime
    developer: str | None = None

    model_config = {"from_attributes": True}


# --- Decision ---

class DecisionCreate(BaseModel):
    session_id: str
    title: str
    chosen: str
    rejected: str | None = None
    reasoning: str = ""
    files: list[str] = Field(default_factory=list)


class DecisionResponse(BaseModel):
    id: str
    session_id: str
    title: str
    chosen: str
    rejected: str | None = None
    reasoning: str
    files: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Commit ---

class CommitCreate(BaseModel):
    session_id: str
    commit_hash: str
    message: str = ""


class CommitResponse(BaseModel):
    id: str
    session_id: str
    commit_hash: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Override Request ---

class OverrideRequestCreate(BaseModel):
    requester_session_id: str
    conflicting_pattern: str
    justification: str = ""


class OverrideRequestResponse(BaseModel):
    id: str
    requester_session_id: str
    owner_session_id: str
    conflicting_pattern: str
    justification: str
    status: str  # pending|approved|denied|expired
    response_message: str | None = None
    created_at: datetime
    responded_at: datetime | None = None
    expires_at: datetime
    requester_developer: str | None = None
    owner_developer: str | None = None

    model_config = {"from_attributes": True}


class OverrideRequestRespond(BaseModel):
    approved: bool
    message: str = ""
