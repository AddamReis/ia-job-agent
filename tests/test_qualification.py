"""Regras configuráveis de remoto, moeda, abrangência nacional e recência."""
import json
from datetime import datetime, timedelta

from src.agents.qualification import (
    JobQualificationAgent,
    currency_rejection_reason,
    detect_currencies,
    explicit_currency_codes,
    is_remote_location,
    stack_mismatch_reason,
)
from src.core.config import Settings
from src.core.state import JobStatus
from src.models.domain import CandidateProfile, JobOpportunity
from src.models.schemas import ExtractedJobFacts, JobFitAssessment


class StubLLM:
    def __init__(self, facts: ExtractedJobFacts) -> None:
        self.facts = facts

    def structured(self, messages, schema):
        return self.facts


class StubRepo:
    def update_job(self, job: JobOpportunity) -> None:
        return None


def _agent(facts: ExtractedJobFacts) -> JobQualificationAgent:
    profile = CandidateProfile(
        years_experience=7,
        skills={"backend": [".NET", "C#", "SQL Server", "AWS"]},
    )
    return JobQualificationAgent(StubLLM(facts), profile, StubRepo(), Settings.load())


def _facts(**overrides) -> ExtractedJobFacts:
    data = {
        "required_skills": [".NET", "C#"],
        "seniority": "senior",
        "years_required": 5,
        "salary_present": True,
        "currency": "USD",
        "is_remote": True,
        "location_eligible": True,
        "language_requirements": ["english"],
        "summary": "Remote .NET engineer role.",
    }
    data.update(overrides)
    return ExtractedJobFacts(**data)


def _job(location: str, salary_raw: str, **overrides) -> JobOpportunity:
    data = {
        "title": ".NET Engineer",
        "company": "Example",
        "location": location,
        "salary_raw": salary_raw,
        "description": "Work with .NET, C#, AWS and SQL Server.",
        "posted_at": datetime.now() - timedelta(hours=2),
        "posted_precision": "hour",
    }
    data.update(overrides)
    return JobOpportunity(**data)


def _fit_agent(
    assessment: JobFitAssessment, facts: ExtractedJobFacts | None = None
) -> JobQualificationAgent:
    """Agente com analista semântico controlado (o StubLLM só devolve fatos)."""
    resolved = facts if facts is not None else _facts()

    class FitLLM:
        def structured(self, messages, schema):
            if schema is JobFitAssessment:
                return assessment
            return resolved

    profile = CandidateProfile(
        years_experience=7,
        skills={"backend": [".NET", "C#", "SQL Server", "AWS"]},
    )
    return JobQualificationAgent(FitLLM(), profile, StubRepo(), Settings.load())


def test_remote_and_usd_are_approved():
    job = _job("Brazil (Remote)", "US$ 120,000 - US$ 150,000")
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "APPLY"
    assert job.score >= 70


def test_generic_work_authorization_does_not_reject_brazilian_city():
    """Vaga remota em Manaus/AM não vira 'autorização não confirmada'."""
    job = _job("Manaus, AM (Remote)", "US$ 120,000")
    _agent(_facts(work_authorization_required=True)).qualify(job)
    assert job.qualification.decision == "APPLY"
    assert "autorização" not in " ".join(job.qualification.reasons).lower()


def test_hybrid_is_rejected_even_with_usd_salary():
    job = _job("São Paulo, SP (Hybrid)", "US$ 120,000")
    _agent(_facts(is_remote=False, location_eligible=False)).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "remoto" in job.qualification.hard_reject_reason


def test_brl_salary_is_rejected():
    job = _job("Brazil (Remote)", "R$ 20.000")
    _agent(_facts(currency="BRL")).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "BRL" in job.currency_signal
    assert "não elegível" in job.qualification.hard_reject_reason


def test_brazilian_job_without_explicit_currency_is_neutral():
    """Ausência de salário não é moeda vedada; só moeda explícita filtra."""
    job = _job("Brasil - Manaus, AM (Remote)", "")
    _agent(_facts(currency=None, salary_present=False)).qualify(job)
    assert job.qualification.decision == "APPLY"
    assert job.qualification.hard_reject_reason is None


def test_brazilian_job_with_explicit_usd_is_allowed():
    job = _job("Brasil - Remoto", "")
    job.description = "empresa paga salário em USD; trabalho com .NET e C#."
    _agent(_facts(currency=None, salary_present=True)).qualify(job)
    assert job.qualification.decision == "APPLY"
    assert job.currency_signal == "USD"


def test_international_job_without_salary_is_presumed_usd_eur():
    job = _job("Berlin, Germany (Remote)", "")
    _agent(_facts(currency=None, salary_present=False)).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_portuguese_posting_without_currency_is_neutral():
    job = _job("Remote", "")
    job.description = "Desenvolvedor .NET remoto, salário a combinar, currículo via LinkedIn."
    _agent(_facts(currency=None, salary_present=False)).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_semantic_rejection_is_explained_in_restriction_details():
    """Score alto sem restrição ainda explica o SKIP na coluna de detalhes."""
    job = _job("São Paulo, SP (Hybrid)", "US$ 120,000")
    _agent(_facts(is_remote=False, location_eligible=False)).qualify(job)
    assert job.status.value == "REJECTED_BY_FILTER"
    assert job.has_restriction is False
    assert job.restriction_kinds == []
    assert job.restriction_details.startswith("análise:")
    assert "remoto" in job.restriction_details.lower()
    assert job.qualification.hard_reject_reason


def test_semantic_rejection_details_only_describe_the_job_side():
    """O detalhe diz o que a vaga exige, não reexplica o perfil do candidato."""
    verbose = (
        "A vaga exige Node.js/TypeScript como core, mas o candidato tem 7 anos "
        "de C#/.NET e não possui experiência relevante com Node."
    )

    class FitLLM:
        def structured(self, messages, schema):
            if schema is JobFitAssessment:
                return JobFitAssessment(
                    core_language="Node.js/TypeScript",
                    language_fit="misaligned",
                    language_reason=verbose,
                    technical_fit=0.2,
                    core_stack=["Node.js", "TypeScript", "NestJS"],
                )
            return _facts(required_skills=["Node.js", "TypeScript"])

    profile = CandidateProfile(
        years_experience=7, skills={"backend": [".NET", "C#", "SQL Server", "AWS"]}
    )
    agent = JobQualificationAgent(FitLLM(), profile, StubRepo(), Settings.load())
    job = _job("Brazil (Remote)", "US$ 120,000", title="Node.js Engineer")
    agent.qualify(job)

    assert job.status.value == "REJECTED_BY_FILTER"
    assert job.restriction_details.startswith("análise: A vaga exige Node.js/TypeScript")
    assert "candidato" not in job.restriction_details.lower()
    assert "7 anos" not in job.restriction_details
    # O cross-check completo continua disponível para auditoria.
    assert job.fit_assessment is not None
    assert job.fit_assessment.language_reason == verbose


def test_java_focused_job_is_rejected():
    """Java como core reprova, mesmo que a descrição cite React como plus."""
    assessment = JobFitAssessment(
        core_language="Java",
        language_fit="misaligned",
        language_reason="A vaga exige Java como linguagem principal (core).",
        technical_fit=0.3,
        core_stack=["Java", "Spring Boot", "Hibernate"],
    )
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        title="Senior Java Developer",
        description="Core Java 17, Spring Boot and Hibernate. React is a plus.",
    )
    _fit_agent(assessment, _facts(required_skills=["Java", "Spring Boot"])).qualify(job)

    assert job.qualification.decision == "SKIP"
    assert "Java" in job.qualification.hard_reject_reason
    assert "linguagem principal" in job.qualification.hard_reject_reason


def test_frontend_focused_job_is_rejected():
    """React/UI como core reprova, ainda que o score consiga ser alto."""
    assessment = JobFitAssessment(
        core_language="React/TypeScript",
        language_fit="misaligned",
        language_reason="A vaga exige React/TypeScript como core.",
        technical_fit=0.9,
        core_stack=["React", "TypeScript", "Next.js", "CSS"],
    )
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        title="Senior Frontend Developer",
        description="React, Next.js, TypeScript and CSS. Node.js only for tooling.",
    )
    _fit_agent(assessment, _facts(required_skills=["React", "TypeScript"])).qualify(job)

    assert job.qualification.decision == "SKIP"
    assert "FrontEnd" in job.qualification.hard_reject_reason


def test_frontend_focus_with_generic_title_uses_extracted_facts():
    assessment = JobFitAssessment(
        core_language="",
        language_fit="aligned",
        technical_fit=0.9,
        core_stack=[],
    )
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        title="Software Engineer",
        description="Build interfaces with React and Next.js.",
    )
    _fit_agent(assessment, _facts(required_skills=["React", "Next.js"])).qualify(job)

    assert job.qualification.decision == "SKIP"
    assert "FrontEnd" in job.qualification.hard_reject_reason


def test_java_complement_in_dotnet_job_is_kept():
    assessment = JobFitAssessment(
        core_language="C#/.NET",
        language_fit="complementary",
        technical_fit=0.9,
        core_stack=[".NET 8", "C#", "SQL Server", "Java integration"],
    )
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        title=".NET Developer",
        description=(
            "Build .NET 8 APIs with C#. Java integration experience is a plus."
        ),
    )
    _fit_agent(assessment).qualify(job)

    assert job.qualification.decision == "APPLY"
    assert job.qualification.hard_reject_reason is None


def test_react_complement_in_fullstack_dotnet_job_is_kept():
    assessment = JobFitAssessment(
        core_language="C#/.NET",
        language_fit="complementary",
        technical_fit=0.9,
        core_stack=[".NET 8", "C#", "React", "SQL Server"],
    )
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        title="Senior Full Stack Developer (ReactJS + .NET C# WebAPI)",
        description="Full stack .NET with React front end and C# WebAPI.",
    )
    _fit_agent(assessment).qualify(job)

    assert job.qualification.decision == "APPLY"
    assert job.qualification.hard_reject_reason is None


def test_dotnet_title_protects_against_wrong_misaligned_verdict():
    """Erro do analista em vaga .NET não pode descartar a vaga."""
    assessment = JobFitAssessment(
        core_language="Java",
        language_fit="misaligned",
        technical_fit=0.9,
        core_stack=["Java", "Spring Boot"],
    )
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        title="Senior .NET Developer",
        description="C#/.NET core. Java knowledge is a plus for integrations.",
    )
    _fit_agent(assessment).qualify(job)

    assert job.qualification.decision == "APPLY"
    assert job.qualification.hard_reject_reason is None


def test_national_remote_with_usd_is_allowed():
    job = _job("São Paulo, SP (Remote)", "USD 120,000")
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_batch_stops_after_two_consecutive_llm_errors():
    """Timeout da DGX não descarta vagas nem gira em falso na fila inteira."""
    from src.core.errors import LLMError

    class Repo:
        def __init__(self, jobs):
            self.jobs = {job.id: job for job in jobs}

        def get_job(self, job_id):
            return self.jobs.get(job_id)

    jobs = [_job("Remote", "USD 100,000") for _ in range(3)]
    for index, job in enumerate(jobs, start=1):
        job.id = f"job-{index}"
    agent = JobQualificationAgent(
        StubLLM(_facts()),
        CandidateProfile(years_experience=7, skills={"backend": [".NET"]}),
        Repo(jobs),
        Settings.load(),
    )

    def explode(job):
        raise LLMError("Todos os backends falharam: APITimeoutError: Request timed out")

    agent._process_one = explode  # type: ignore[method-assign]
    assert agent.run(job_ids=[job.id for job in jobs]) == 0
    assert "abortado em 2/3" in agent.last_error


def test_currency_detection_does_not_confuse_brl_with_usd():
    assert detect_currencies("R$ 20.000") == {"BRL"}
    assert detect_currencies("US$ 120,000") == {"USD"}
    assert detect_currencies("€ 90,000") == {"EUR"}
    assert detect_currencies("CHF 123'000 - CHF 153'000") == {"CHF"}


def test_explicit_iso_codes_salvage_currency_extracted_by_llm():
    assert explicit_currency_codes("CHF", None, "usd") == {"CHF", "USD"}
    assert explicit_currency_codes("moeda não informada", "") == set()


def test_unknown_currency_rejects_but_missing_salary_is_neutral():
    allowed = {"USD", "EUR"}
    assert "CHF" in currency_rejection_reason({"CHF"}, allowed, national=False)
    assert currency_rejection_reason(set(), allowed, national=True) is None


def test_remote_detection_rejects_hybrid_marker():
    assert is_remote_location("Brazil (Remote)")
    assert not is_remote_location("São Paulo, SP (Hybrid)")


def test_posting_older_than_four_days_is_rejected():
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        posted_at=datetime.now() - timedelta(hours=97),
        posted_precision="hour",
    )
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "96 horas" in job.qualification.hard_reject_reason


def test_posting_between_6_and_96_hours_is_kept():
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        posted_at=datetime.now() - timedelta(hours=72),
        posted_precision="hour",
    )
    _agent(_facts()).qualify(job)
    assert job.status == JobStatus.QUALIFIED


def test_posting_without_explicit_recency_is_rejected():
    job = _job("Brazil (Remote)", "USD 120,000", posted_at=None, posted_precision="unknown")
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "96 horas" in job.qualification.hard_reject_reason


def test_fresh_posting_within_96_hours_is_approved():
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        posted_at=datetime.now() - timedelta(minutes=30),
        posted_precision="hour",
    )
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "APPLY"


class CountingLLM:
    def __init__(self, facts: ExtractedJobFacts) -> None:
        self.facts = facts
        self.calls = 0

    def structured(self, messages, schema):
        self.calls += 1
        return self.facts


class RecordingRepo:
    def __init__(self, jobs: list[JobOpportunity]) -> None:
        self.jobs = jobs

    def list_jobs(self, status, limit=None):
        return list(self.jobs)

    def update_job(self, job: JobOpportunity) -> None:
        return None


def test_stale_jobs_are_rejected_without_calling_llm():
    stale = _job(
        "Brazil (Remote)",
        "USD 120,000",
        posted_at=datetime.now() - timedelta(hours=97),
        posted_precision="hour",
    )
    fresh = _job(
        "Brazil (Remote)",
        "USD 120,000",
        posted_at=datetime.now() - timedelta(hours=1),
        posted_precision="hour",
    )
    llm = CountingLLM(_facts())
    profile = CandidateProfile(
        years_experience=7, skills={"backend": [".NET", "C#", "SQL Server", "AWS"]}
    )
    agent = JobQualificationAgent(llm, profile, RecordingRepo([stale, fresh]), Settings.load())

    agent.run()

    assert stale.status == JobStatus.EXPIRED
    assert "96 horas" in stale.qualification.hard_reject_reason
    assert fresh.status == JobStatus.QUALIFIED
    # Somente a vaga fresca vai à LLM: 1 extração + 1 analista semântico.
    assert llm.calls == 2


def test_ignore_recency_reanalyzes_stale_job():
    """A reclassificação de auditoria pode desativar a janela de defasagem."""
    stale = _job(
        "Brazil (Remote)",
        "USD 120,000",
        posted_at=datetime.now() - timedelta(hours=100),
        posted_precision="hour",
    )
    llm = CountingLLM(_facts())
    profile = CandidateProfile(
        years_experience=7, skills={"backend": [".NET", "C#", "SQL Server", "AWS"]}
    )
    agent = JobQualificationAgent(llm, profile, RecordingRepo([stale]), Settings.load())
    agent.ignore_recency = True

    agent.run()

    assert stale.status == JobStatus.QUALIFIED
    assert llm.calls == 2
    assert "96 horas" not in json.dumps(stale.qualification.reasons, ensure_ascii=False)



def test_explicit_leadership_title_is_rejected():
    job = _job("Brazil (Remote)", "US$ 120,000", title="Technical Delivery Manager")
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "liderança explícita" in job.qualification.hard_reject_reason


def test_explicit_tech_lead_in_description_is_rejected():
    facts = _facts(
        explicit_leadership_role=True,
        leadership_evidence="You will be the tech lead of the squad",
    )
    job = _job("Brazil (Remote)", "US$ 120,000")
    _agent(facts).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "liderança explícita" in job.qualification.hard_reject_reason


def test_mentoring_is_not_explicit_leadership():
    job = _job(
        "Brazil (Remote)", "US$ 120,000",
        description="Mentor other engineers and provide technical guidance.",
    )
    _agent(_facts(explicit_leadership_role=False)).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_required_sector_missing_rejected_but_preferred_not():
    job = _job("Brazil (Remote)", "US$ 120,000")
    _agent(_facts(required_domain_experience=["healthcare"])).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "setor ausente" in job.qualification.hard_reject_reason

    job2 = _job("Brazil (Remote)", "US$ 120,000")
    _agent(_facts(preferred_domain_experience=["healthcare"])).qualify(job2)
    assert job2.qualification.decision == "APPLY"


def test_covered_sector_requirement_is_approved():
    job = _job("Brazil (Remote)", "US$ 120,000")
    _agent(_facts(required_domain_experience=["insurance"])).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_level_without_specification_has_priority_over_senior():
    no_level = _job("Brazil (Remote)", "US$ 120,000")
    _agent(_facts(seniority="unknown")).qualify(no_level)
    senior = _job("Brazil (Remote)", "US$ 120,000")
    _agent(_facts(seniority="senior")).qualify(senior)
    assert no_level.qualification.decision == "APPLY"
    assert senior.qualification.decision == "APPLY"
    assert no_level.level_priority == 0
    assert senior.level_priority == 2
    assert no_level.level_priority < senior.level_priority


def test_ai_core_is_rejected_but_complement_in_dotnet_job_is_not():
    ai_title = _job(
        "Brazil (Remote)", "US$ 120,000",
        title="AI Full-stack Engineer (Next + Python)",
    )
    reason = stack_mismatch_reason(ai_title, _facts(), JobFitAssessment())
    assert reason is not None and "Inteligência Artificial" in reason

    generic = _job("Brazil (Remote)", "US$ 120,000", title="Software Engineer")
    reason = stack_mismatch_reason(
        generic, _facts(), JobFitAssessment(core_stack=["Machine Learning", "PyTorch"])
    )
    assert reason is not None and "Inteligência Artificial" in reason

    dotnet = _job("Brazil (Remote)", "US$ 120,000", title="Senior .NET Engineer")
    facts = _facts(required_skills=[".NET", "C#", "Azure OpenAI integration"])
    assert stack_mismatch_reason(
        dotnet, facts, JobFitAssessment(core_stack=[".NET", "C#"])
    ) is None



def test_hybrid_marker_vetoes_even_when_llm_says_remote():
    """Dupla validação: o texto público vence o is_remote do LLM."""
    job = _job(
        "Chicago, IL", "US$ 120,000",
        title="Senior Software Engineer",
        description="Work Model: Hybrid. Three days in office.",
    )
    _agent(_facts(is_remote=True)).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "remoto" in job.qualification.hard_reject_reason


def test_technical_hybrid_cloud_does_not_veto_remote():
    job = _job(
        "Brazil (Remote)", "US$ 120,000",
        title=".NET Engineer",
        description="100% remote role. Experience with hybrid cloud environments.",
    )
    _agent(_facts(is_remote=True)).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_public_api_remote_filter():
    from src.execution.web.linkedin_public import is_remote_public

    assert is_remote_public(".NET Engineer", "Brazil (Remote)", "Work from home")
    assert not is_remote_public("Engineer", "Chicago, IL", "Work Model: Hybrid")
    assert not is_remote_public("Engineer", "São Paulo (Presencial)", "")
    assert not is_remote_public("Engineer", "Brazil", "")
    assert is_remote_public(
        "Engineer", "United States", "100% remote. Hybrid cloud experience."
    )



def test_easy_apply_defaults_to_unanalyzed():
    """Easy Apply so é descoberto no celular; default é None (não analisado)."""
    job = JobOpportunity(title="Engineer", company="Acme", external_id="4491234567")
    assert job.easy_apply is None
    assert job.easy_apply_source == ""


def test_detect_region_latam_priority():
    from src.agents.qualification import detect_region

    assert detect_region("LATAM") == "latam"
    assert detect_region("Mexico (Remote)") == "latam"
    # Brasil mantém sua região própria; LATAM é um termo adicional, não substituto.
    assert detect_region("São Paulo, Brazil") == "brazil"
    assert detect_region("Manaus, AM") == "brazil"
    assert detect_region("United States") == "americas_north"


def test_dedupe_uses_real_job_id_only():
    from src.storage.database import dedupe_hash

    a = dedupe_hash("Acme", "Engineer", "Brazil", external_id="4491234567")
    b = dedupe_hash("Acme", "Senior Engineer", "Remote", external_id="4491234567")
    c = dedupe_hash("Acme", "Engineer", "Brazil", external_id="4499999999")
    assert a == b
    assert a != c


def test_classification_does_not_probe_easy_apply():
    """Easy Apply só é detectado depois, na abertura usada pela candidatura."""
    job = _job(
        "Brazil (Remote)",
        "USD 120,000",
        external_id="4491746367",
        apply_marker="Easy Apply",
    )
    _agent(_facts()).qualify(job)
    assert job.qualification.decision == "APPLY"
    # Nem o marker do card nem a classificação abrem o Chrome logado.
    assert job.easy_apply is None
    assert job.easy_apply_source == ""
    assert job.channel == "unknown"

