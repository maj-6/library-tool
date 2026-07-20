"""Strict mirror of Android's v1 post-processing request contract."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

Role = Literal["title_page", "cover", "spine", "other"]
Outcome = Literal[
    "page_dewarp",
    "detected_margin_crop",
    "contrast_normalization",
    "spine_crop",
]


class ContractError(ValueError):
    """A request is structurally valid JSON but violates the Android contract."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProcessingFeatures(StrictModel):
    page_dewarp: bool
    detected_margin_crop: bool
    contrast_normalization: bool
    spine_crop: bool


_TUNING = {
    "modern": (55, 2, 70, 25),
    "older": (70, 4, 50, 75),
    "early": (85, 8, 35, 90),
    "unknown_date": (65, 5, 45, 75),
}


class ProcessingProfile(StrictModel):
    version: Literal[1]
    selected_preset: Literal[
        "automatic_by_date",
        "modern_1950_and_later",
        "older_1850_to_1949",
        "early_before_1850",
    ]
    resolved_treatment: Literal["modern", "older", "early", "unknown_date"]
    publication_year: int | None
    features: ProcessingFeatures
    page_dewarp_strength_percent: int = Field(ge=0, le=100)
    detected_margin_padding_percent: int = Field(ge=0, le=100)
    contrast_strength_percent: int = Field(ge=0, le=100)
    paper_tone_retention_percent: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_android_resolution(self) -> "ProcessingProfile":
        year = self.publication_year
        if year is not None and not 1 <= year <= 9999:
            raise ContractError("publication_year must be null or between 1 and 9999")
        if self.selected_preset == "automatic_by_date":
            expected = (
                "unknown_date"
                if year is None
                else "early"
                if year < 1850
                else "older"
                if year < 1950
                else "modern"
            )
        else:
            expected = {
                "modern_1950_and_later": "modern",
                "older_1850_to_1949": "older",
                "early_before_1850": "early",
            }[self.selected_preset]
        if self.resolved_treatment != expected:
            raise ContractError("resolved_treatment does not match preset and publication year")
        actual = (
            self.page_dewarp_strength_percent,
            self.detected_margin_padding_percent,
            self.contrast_strength_percent,
            self.paper_tone_retention_percent,
        )
        if actual != _TUNING[expected]:
            raise ContractError("profile tuning does not match Android's version 1 preset")
        return self


class ProcessingSource(StrictModel):
    asset_id: str = Field(min_length=1, max_length=160, pattern=SAFE_TOKEN.pattern)
    role: Role
    original_sha256: str = Field(pattern=SHA256.pattern)
    original_revision: int = Field(ge=1)
    display_sha256: str = Field(pattern=SHA256.pattern)
    display_revision: int = Field(ge=1)


class ProcessingOperation(StrictModel):
    outcome: Outcome
    result_role: Role | None

    @model_validator(mode="after")
    def validate_result_role(self) -> "ProcessingOperation":
        if self.outcome == "spine_crop":
            if self.result_role != "spine":
                raise ContractError("spine_crop must declare result_role=spine")
        elif self.result_role is not None:
            raise ContractError("only spine_crop may declare a result role")
        return self


class ProcessingRequest(StrictModel):
    schema_name: Literal["org.whl.bookcapture.photo-processing-request"] = Field(alias="schema")
    version: Literal[1]
    request_id: str = Field(min_length=1, max_length=160, pattern=SAFE_TOKEN.pattern)
    request_revision: int = Field(ge=1)
    profile: ProcessingProfile
    requested_at: int = Field(gt=0)
    status: Literal["requested"]
    source: ProcessingSource
    operations: list[ProcessingOperation] = Field(min_length=1, max_length=4)
    result: None

    @model_validator(mode="after")
    def validate_recipe(self) -> "ProcessingRequest":
        outcomes = [operation.outcome for operation in self.operations]
        if len(outcomes) != len(set(outcomes)):
            raise ContractError("processing outcomes must be unique")
        role = self.source.role
        features = self.profile.features
        expected: list[str] = []
        if role in {"title_page", "cover"}:
            if features.page_dewarp:
                expected.append("page_dewarp")
            if features.detected_margin_crop:
                expected.append("detected_margin_crop")
            if features.contrast_normalization:
                expected.append("contrast_normalization")
        elif role == "spine":
            if features.contrast_normalization:
                expected.append("contrast_normalization")
            if features.spine_crop:
                expected.append("spine_crop")
        if outcomes != expected:
            raise ContractError("operations do not match the role and enabled feature gates")
        return self


class JobRecord(StrictModel):
    id: str
    capture_id: str
    owner_id: str
    asset_id: str
    request_id: str
    request_revision: int
    source_path: str
    source_sha256: str
    state: Literal["queued", "running", "retrying", "completed", "failed", "cancelled"]
    attempt_count: int
    request: dict

    def parsed_request(self) -> ProcessingRequest:
        request = ProcessingRequest.model_validate(self.request)
        if request.request_id != self.request_id:
            raise ContractError("job request_id does not match its request snapshot")
        if request.request_revision != self.request_revision:
            raise ContractError("job request revision does not match its request snapshot")
        if request.source.asset_id != self.asset_id:
            raise ContractError("job asset_id does not match its request snapshot")
        if request.source.original_sha256 != self.source_sha256:
            raise ContractError("job source checksum does not match its request snapshot")
        return request
