import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UploadOut(BaseModel):
    upload_id: uuid.UUID
    width: int
    height: int
    quality_warnings: list[str] = Field(default_factory=list)


class JobCreate(BaseModel):
    source_upload_id: uuid.UUID
    style: str
    mode: Literal["same_angle"] = "same_angle"   # novel angles are a later, separately evaluated mode
    consent_version: str = Field(min_length=1, max_length=32)


class JobAccepted(BaseModel):
    job_id: uuid.UUID
    status: str
    status_url: str


class JobOut(BaseModel):
    job_id: uuid.UUID
    status: str
    style: str
    mode: str
    attempt_count: int
    error_code: str | None = None
    message: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    ai_generated: bool = True


class ResultImage(BaseModel):
    url: str
    expires_in: int
    provider: str
    label: str = "AI-edited studio image. Lighting and background are generated."


class JobResults(BaseModel):
    job_id: uuid.UUID
    status: str
    images: list[ResultImage]


class ReviewDecision(BaseModel):
    decision: Literal["approve", "reject", "request_more_images"]
    reason: str | None = Field(default=None, max_length=500)
    reviewer_id: str = Field(min_length=1, max_length=64)
