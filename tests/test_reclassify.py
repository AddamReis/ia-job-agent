"""Rota de reclassificação por Trace ID e bypass de supressão em dados existentes."""
from src.agents.orchestrator import Orchestrator
from src.core.state import JobStatus
from src.models.domain import JobOpportunity


TRACE_A = "trace-a"
TRACE_B = "trace-b"


class FakeRepo:
    def __init__(self, jobs: list[JobOpportunity]) -> None:
        self.jobs = {job.id: job for job in jobs}

    def list_jobs(self, status=None, limit=None, trace_id=None):
        items = list(self.jobs.values())
        if status is not None:
            statuses = status if isinstance(status, list) else [status]
            items = [job for job in items if job.status in statuses]
        if trace_id:
            items = [job for job in items if job.trace_id == trace_id]
        if limit:
            items = items[:limit]
        return items

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def update_job(self, job: JobOpportunity) -> None:
        self.jobs[job.id] = job


class FakeQualification:
    def __init__(self, suppression: object) -> None:
        self.suppression = suppression
        self.seen: list[str] | None = None
        self.suppression_during_run: object = object()
        self.ignore_recency = False
        self.ignore_recency_during_run: bool | None = None

    def run(self, limit=None, job_ids=None) -> int:
        self.seen = job_ids
        self.suppression_during_run = self.suppression
        self.ignore_recency_during_run = self.ignore_recency
        return len(job_ids or [])


def _orchestrator(jobs: list[JobOpportunity]):
    orch = Orchestrator.__new__(Orchestrator)
    orch.repo = FakeRepo(jobs)
    suppression = object()
    orch.qualification = FakeQualification(suppression)
    return orch, suppression


def _job(trace_id: str, status: JobStatus) -> JobOpportunity:
    return JobOpportunity(trace_id=trace_id, status=status, company="Example", title="Dev")


def test_search_only_does_not_start_live_qualification():
    from types import SimpleNamespace

    class FakeResearch:
        def __init__(self) -> None:
            self.trace_id = ""
            self.runs = 0

        def run(self) -> None:
            self.runs += 1

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = SimpleNamespace(search=SimpleNamespace(source="browser"))
    orch.browser_research = FakeResearch()
    orch.research = FakeResearch()
    orch.current_trace_id = "trace-search"

    orch._run_search_only()

    assert orch.browser_research.runs == 1
    assert orch.research.runs == 0
    assert orch.browser_research.trace_id == "trace-search"


def test_reclassify_by_trace_only_queues_that_trace():
    jobs = [
        _job(TRACE_A, JobStatus.QUALIFIED),
        _job(TRACE_A, JobStatus.REJECTED_BY_FILTER),
        _job(TRACE_B, JobStatus.QUALIFIED),
    ]
    orch, suppression = _orchestrator(jobs)
    aprovadas = orch.reclassify_jobs(trace_id=TRACE_A)

    assert aprovadas == 2
    assert set(orch.qualification.seen or []) == {jobs[0].id, jobs[1].id}
    assert orch.qualification.suppression_during_run is None
    assert orch.qualification.suppression is suppression
    assert all(job.status == JobStatus.DISCOVERED for job in jobs[:2])
    assert jobs[2].status == JobStatus.QUALIFIED


def test_reclassify_all_includes_rejected_and_expired_but_not_submitted():
    jobs = [
        _job(TRACE_A, JobStatus.REJECTED_BY_FILTER),
        _job(TRACE_A, JobStatus.EXPIRED),
        _job(TRACE_A, JobStatus.SUBMITTED),
    ]
    orch, _ = _orchestrator(jobs)
    aprovadas = orch.reclassify_jobs(all_statuses=True)

    assert aprovadas == 2
    assert set(orch.qualification.seen or []) == {jobs[0].id, jobs[1].id}
    assert jobs[2].status == JobStatus.SUBMITTED


def test_reclassify_ignore_recency_is_scoped_to_the_run():
    jobs = [_job(TRACE_A, JobStatus.EXPIRED)]
    orch, _ = _orchestrator(jobs)

    orch.reclassify_jobs(statuses=[JobStatus.EXPIRED], ignore_recency=True)

    assert orch.qualification.ignore_recency_during_run is True
    assert orch.qualification.ignore_recency is False


def test_reclassify_default_keeps_original_status_filter():
    jobs = [
        _job(TRACE_A, JobStatus.QUALIFIED),
        _job(TRACE_A, JobStatus.REJECTED_BY_FILTER),
    ]
    orch, _ = _orchestrator(jobs)
    aprovadas = orch.reclassify_jobs(statuses=[JobStatus.QUALIFIED])

    assert aprovadas == 1
    assert orch.qualification.seen == [jobs[0].id]
