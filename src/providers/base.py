"""Common posting shape. Every provider adapter returns a list of these."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Posting:
    provider: str
    company: str
    job_id: str
    title: str
    location: str
    url: str
    posted_at: Optional[str] = None   # ISO8601 string, best-effort
    department: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        """Stable dedupe key. Survives title edits and re-postings."""
        return f"{self.provider}:{self.company}:{self.job_id}"

    def haystack(self) -> str:
        """Text that keyword matching runs against."""
        return " \n ".join(filter(None, [self.title, self.department, self.description]))
