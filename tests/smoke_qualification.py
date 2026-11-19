"""Smoke test real: extrai a descrição de um PDF e pontua com o LLM local."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.orchestrator import Orchestrator
from src.core.config import Settings
from src.core.events import EventBus
from src.models.domain import JobOpportunity

PDF = Path(
    r"C:\Users\addam\Downloads\Job Description - HealthEquity - Identity Backend Developer B. 102809.pdf"
)


def main() -> None:
    settings = Settings.load()
    orchestrator = Orchestrator(settings, EventBus())
    import pymupdf

    with pymupdf.open(PDF) as document:
        description = "\n".join(page.get_text() for page in document)

    job = JobOpportunity(
        title="Identity Backend Developer",
        company="HealthEquity",
        location="United States",
        description=description,
    )
    result = orchestrator.qualification.qualify(job)
    print("STATUS:", result.status.value)
    print("SCORE:", result.score)
    print("BREAKDOWN:", result.score_breakdown)
    print("REASONS:", result.qualification.reasons if result.qualification else [])
    print("RESUMO:", result.analysis.summary if result.analysis else "")


if __name__ == "__main__":
    main()
