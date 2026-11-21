from __future__ import annotations

import re
from pathlib import Path

_PATH_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)


def job_dir(app_data: Path, project: str, job: str) -> Path:
    """Return the canonical, contained directory for a project job."""
    if _PATH_ID.fullmatch(project) is None or _PATH_ID.fullmatch(job) is None:
        raise ValueError("invalid job id")
    projects = (app_data / "projects").resolve()
    project_dir = (projects / project).resolve()
    jobs = (project_dir / "jobs").resolve()
    candidate = (jobs / job).resolve()
    try:
        candidate.relative_to(jobs)
    except ValueError:
        raise ValueError("invalid job id") from None
    return candidate
