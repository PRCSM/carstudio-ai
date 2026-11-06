import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UploadOut(BaseModel):
    upload_id: uuid.UUID
    width: int
    height: int
    quality_warnings: list[str] = Field(default_factory=list)
    group_id: uuid.UUID | None = None          # album / single-photo group for /v1/sets
    is_first_in_group: bool = True             # only the first photo of an album should prompt the customer
    group_size: int = 1


class JobCreate(BaseModel):
    source_upload_id: uuid.UUID
    style: str
    mode: Literal["same_angle"] = "same_angle"   # novel angles go through /v1/sets (mode 'angles')
    consent_version: str = Field(min_length=1, max_length=32)
    hide_plate: bool = False


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


class SetCreate(BaseModel):
    group_id: uuid.UUID
    mode: Literal["enhance", "angles"]   # enhance = studio-edit each real photo; angles = AI-generated viewpoints
    style: str
    hide_plate: bool = False
    consent_version: str = Field(min_length=1, max_length=32)


class SetAccepted(BaseModel):
    set_id: uuid.UUID
    status: str
    total_jobs: int
    status_url: str


class SetItem(BaseModel):
    job_id: uuid.UUID
    angle: str | None
    status: str
    message: str | None = None
    url: str | None = None
    ai_visualization: bool = False


class SetOut(BaseModel):
    set_id: uuid.UUID
    mode: str
    style: str
    status: str
    hide_plate: bool
    total_jobs: int
    items: list[SetItem]
    expires_in: int
