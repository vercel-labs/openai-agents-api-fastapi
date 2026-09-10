from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox_agent.models import InspectionRequest


def test_accepts_public_github_repository() -> None:
    request = InspectionRequest(
        repo_url="https://github.com/example/project",
        ref="main",
        question="How is this project structured?",
    )

    assert request.repo_url == "https://github.com/example/project"
    assert request.question == "How is this project structured?"


@pytest.mark.parametrize(
    "repo_url",
    [
        "git@github.com:example/project.git",
        "http://github.com/example/project",
        "https://token@github.com/example/project",
        "https://git.example.com/example/project",
        "https://github.com/example/project?token=secret",
    ],
)
def test_rejects_non_public_github_urls(repo_url: str) -> None:
    with pytest.raises(ValidationError):
        InspectionRequest(
            repo_url=repo_url,
            question="How is this project structured?",
        )


def test_rejects_option_like_git_ref() -> None:
    with pytest.raises(ValidationError):
        InspectionRequest(
            repo_url="https://github.com/example/project",
            ref="--upload-pack=oops",
            question="How is this project structured?",
        )
