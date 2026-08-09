from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class PRResult:
    ticket_id: str
    repo_id: str
    branch_name: str
    pr_url: str
    pr_number: int
    title: str
    files_changed: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def success(self) -> bool:
        return bool(self.pr_url) and not self.error

    def summary(self) -> str:
        if self.success:
            return (
                f"PR #{self.pr_number} opened: {self.pr_url}\n"
                f"  Branch:  {self.branch_name}\n"
                f"  Files:   {', '.join(self.files_changed)}"
            )
        return f"PR creation failed: {self.error}"
