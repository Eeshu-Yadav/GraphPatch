from dataclasses import dataclass, field


@dataclass
class Ticket:
    ticket_id: str
    title: str
    body: str
    repo_id: str
    labels: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return f"{self.title}\n\n{self.body}"
