from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_url: str = Field(min_length=20, max_length=500)
    ref: str = Field(default="main", min_length=1, max_length=250)
    question: str = Field(min_length=8, max_length=1000)

    @field_validator("repo_url")
    @classmethod
    def public_github_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(path_parts) < 2
        ):
            raise ValueError("Repository must be a public https://github.com/owner/repo URL")
        return normalized

    @field_validator("ref")
    @classmethod
    def safe_git_ref(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or normalized.startswith("-")
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("Git ref is empty or unsafe")
        return normalized

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        return " ".join(value.strip().split())


class Artifact(BaseModel):
    name: str
    media_type: str
    content: str
    truncated: bool = False
