"""Persistência SQLite/Postgres com SQLAlchemy 2.0."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Collection
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    Boolean,
    Integer,
    String,
    Text,
    and_,
    cast,
    create_engine,
    exists,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..core.state import ExecutionStatus, JobStatus
from ..shared.seed import TRUSTED_PORTALS
from ..models.domain import (
    AnswerRecord,
    Application,
    ApplicationDependency,
    ApprovalRequest,
    Company,
    DailyLimit,
    ExecutionHistory,
    JobOpportunity,
    QualificationFeedback,
)


class Base(DeclarativeBase):
    pass


class ProfileRow(Base):
    __tablename__ = "candidate_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class JobRow(Base):
    __tablename__ = "job_opportunities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True, default="", index=True)
    company: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    location: Mapped[str] = mapped_column(String, default="")
    location_normalized: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    url: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="DISCOVERED")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedupe_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    company_id: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    external_apply_url: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    external_apply_source: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    has_restriction: Mapped[bool] = mapped_column(Boolean, default=False)
    restriction_details: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    has_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)
    easy_apply: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=None, index=True
    )
    easy_apply_source: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    salary_disclosed: Mapped[bool] = mapped_column(Boolean, default=False)
    salary_basis: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    salary_text: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    search_query: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    search_location: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    apply_failure_reason: Mapped[str | None] = mapped_column(String, nullable=True, default="")
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ApplicationRow(Base):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True, default="", index=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    company: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="SUBMITTED")
    applied_at: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)


class ExecutionRow(Base):
    __tablename__ = "executions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True, default="", index=True)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="RUNNING")
    data: Mapped[str] = mapped_column(Text, nullable=False)


class RunLockRow(Base):
    """Trava persistente: no máximo um dono por serviço (ex.: ``discovery``)."""

    __tablename__ = "run_locks"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    acquired_at: Mapped[str] = mapped_column(String, nullable=False)
    renewed_at: Mapped[str] = mapped_column(String, nullable=False)


class DailyLimitRow(Base):
    __tablename__ = "daily_limits"
    date: Mapped[str] = mapped_column(String, primary_key=True)
    submitted: Mapped[int] = mapped_column(Integer, default=0)
    limit: Mapped[int] = mapped_column(Integer, default=10)


class CheckpointRow(Base):
    __tablename__ = "run_checkpoints"
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class NotificationStateRow(Base):
    """Estado persistente de e-mails periódicos (evita reenvio no mesmo dia)."""

    __tablename__ = "notification_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class NotificationLogRow(Base):
    """Histórico de e-mails enviados, correlacionado ao trace ID da execução."""

    __tablename__ = "notification_log"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    trace_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subject: Mapped[str] = mapped_column(String, nullable=False, default="")
    message_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    sent_at: Mapped[str] = mapped_column(String, nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approval_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True, default="", index=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ReviewDecisionRow(Base):
    """Decisão tomada na plataforma intermediária (quem decidiu e por quê)."""

    __tablename__ = "review_decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reviewer: Mapped[str] = mapped_column(String, nullable=False, default="operator")
    checks: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class QualificationFeedbackRow(Base):
    """Memória local de classificação: correções humanas e desfechos."""

    __tablename__ = "qualification_feedback"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True, default="", index=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, index=True)
    verdict: Mapped[str] = mapped_column(String, index=True)
    topic: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class AgentEventRow(Base):
    """Outbox persistente: evento entre workers, mesmo após restart ou CDP remoto.

    O id é determinístico ``consumer|event_type|job_id``: publicar o mesmo aviso
    duas vezes é idempotente, e um evento já encerrado pode ser reativado quando
    a vaga volta a ser classificada.
    """

    __tablename__ = "agent_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    consumer: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class CompanyRow(Base):
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    name_normalized: Mapped[str] = mapped_column(String, unique=True, index=True)
    has_account: Mapped[bool] = mapped_column(Boolean, default=False)
    technical_test: Mapped[str] = mapped_column(String, default="unknown")
    is_trusted_portal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    trusted_portal_config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class DiscoveryWatermarkRow(Base):
    """Último corte de cada slot do discovery — evita buraco entre execuções."""

    __tablename__ = "discovery_watermarks"
    slot: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class DependencyRow(Base):
    __tablename__ = "application_dependencies"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True, default="", index=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    application_id: Mapped[str] = mapped_column(String, default="", index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    owner: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


# Colunas adicionadas depois da criação original. `create_all` não altera
# tabelas existentes, então o __init__ aplica estas migrations idempotentes.
_EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "job_opportunities": {
        "company_id": "VARCHAR",
        "external_apply_url": "VARCHAR",
        "external_apply_source": "VARCHAR",
        "has_restriction": "BOOLEAN DEFAULT 0",
        "restriction_details": "VARCHAR",
        "has_applied": "BOOLEAN DEFAULT 0",
        "applied_at": "VARCHAR",
        "easy_apply": "BOOLEAN",  # tri-estado: NULL não analisado / 0 / 1
        "easy_apply_source": "VARCHAR",
        "salary_disclosed": "BOOLEAN DEFAULT 0",
        "salary_basis": "VARCHAR",
        "salary_text": "VARCHAR",
        "search_query": "VARCHAR",
        "search_location": "VARCHAR",
        "apply_failure_reason": "VARCHAR",
        "trace_id": "VARCHAR",
        "location_normalized": "VARCHAR",
    },
    "applications": {"trace_id": "VARCHAR"},
    "companies": {
        "is_trusted_portal": "BOOLEAN DEFAULT 0",
        "trusted_portal_config": "TEXT DEFAULT '{}'",
    },
    "application_dependencies": {"trace_id": "VARCHAR"},
    "approval_requests": {"trace_id": "VARCHAR"},
    "executions": {"trace_id": "VARCHAR"},
}


def _ensure_columns(engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _EXTRA_COLUMNS.items():
        if table not in existing_tables:
            continue  # create_all acabou de criar a tabela com a coluna já incluída.
        existing = {column["name"] for column in inspector.get_columns(table)}
        with engine.begin() as connection:
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    )


def _dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)


def dedupe_hash(company: str, title: str, location: str, external_id: str = "", url: str = "") -> str:
    """Dedupe pelo ID REAL da vaga. Título/localização podem mudar sem criar duplicata."""
    real_id = (external_id or url or "").strip().lower()
    if not real_id:
        # Sem ID real não é seguro deduzir identidade — usa os campos como fallback.
        real_id = "|".join(
            [(company or "").strip().lower(),
             (title or "").strip().lower(),
             (location or "").strip().lower()]
        )
    return hashlib.sha256(real_id.encode("utf-8")).hexdigest()[:32]


class Repository:
    def __init__(
        self,
        db_path: Path | None = None,
        known_portals: Collection[str] | None = None,
        *,
        database_url: str | None = None,
    ) -> None:
        self.known_portals = {
            self.normalize_company(portal) for portal in (known_portals or ())
        }
        url = database_url or os.getenv("AI_JOB_DATABASE_URL")
        if url:
            # Postgres no DGX Spark: DBeaver acessa a mesma fonte pela rede.
            self.engine = create_engine(url, pool_pre_ping=True)
        else:
            db_path = db_path or Path("data/agent.db")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
            )
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            # No Postgres o create_all já cria as colunas finais; o helper de
            # ALTER TABLE é específico da migração incremental do SQLite.
            _ensure_columns(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.Session() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    # ---- perfil ----
    def save_profile(self, profile: BaseModel) -> None:
        with self.session() as session:
            row = session.get(ProfileRow, 1)
            if row is None:
                row = ProfileRow(id=1, data=_dump(profile), updated_at=datetime.now().isoformat())
                session.add(row)
            else:
                row.data = _dump(profile)
                row.updated_at = datetime.now().isoformat()

    def load_profile(self, schema: type[BaseModel]) -> BaseModel | None:
        with self.session() as session:
            row = session.get(ProfileRow, 1)
            if row is None:
                return None
            return schema.model_validate(json.loads(row.data))

    # ---- vagas ----
    def upsert_job(self, job: JobOpportunity) -> bool:
        """Retorna True se a vaga é nova; False se já existia (dedupe)."""
        # O vínculo com a empresa é feito AQUI, na gravação da vaga — não
        # apenas quando a dependência é classificada (que só ocorre em APPLY).
        if job.company.strip() and not job.company_id:
            company = self.ensure_company(job.company)
            if company is not None:
                job.company_id = company.id
        if not job.dedupe_hash:
            job.dedupe_hash = dedupe_hash(
                job.company, job.title, job.location, job.external_id, job.url
            )
        with self.session() as session:
            existing = session.scalar(
                select(JobRow).where(JobRow.dedupe_hash == job.dedupe_hash)
            )
            now = datetime.now().isoformat()
            if existing:
                # Atualiza dados sem sobrescrever estados terminais.
                terminal = {
                    JobStatus.SUBMITTED.value,
                    JobStatus.FAILED.value,
                    JobStatus.REQUIRES_HUMAN_ACTION.value,
                    JobStatus.WAITING_DEVICE.value,
                    JobStatus.PENDING_APPROVAL.value,
                    JobStatus.APPLYING.value,
                }
                if existing.status not in terminal:
                    # O card recém-lido tem um UUID novo e não contém descrição,
                    # score ou restrições. Mescla apenas os campos do card no
                    # registro já enriquecido, preservando o trabalho anterior.
                    current = JobOpportunity.model_validate(json.loads(existing.data))
                    card_fields = (
                        "title", "company", "location", "location_normalized",
                        "url", "external_id",
                        "source", "posted_raw", "posted_at", "posted_precision",
                        "salary_raw", "salary_min", "salary_max", "currency",
                        "employment_type", "apply_marker",
                        "easy_apply", "easy_apply_source",
                    )
                    for field in card_fields:
                        setattr(current, field, getattr(job, field))
                    # A primeira execução que descobriu a vaga é a dona do trace.
                    current.trace_id = current.trace_id or job.trace_id
                    # O mesmo vale para o termo/localidade: a vaga pode reaparecer
                    # em várias queries da mesma rodada, mas a origem registrada
                    # deve ser a da PRIMEIRA descoberta, não a do último reencontro.
                    current.search_query = current.search_query or job.search_query
                    current.search_location = current.search_location or job.search_location
                    current.company_id = job.company_id or current.company_id
                    current.dedupe_hash = job.dedupe_hash
                    existing.trace_id = current.trace_id
                    existing.company_id = current.company_id
                    existing.external_apply_url = current.external_apply_url
                    existing.external_apply_source = current.external_apply_source
                    existing.has_restriction = current.has_restriction
                    existing.restriction_details = current.restriction_details
                    existing.has_applied = current.has_applied
                    existing.easy_apply = current.easy_apply
                    existing.easy_apply_source = current.easy_apply_source
                    existing.applied_at = (
                        current.applied_at.isoformat() if current.applied_at else None
                    )
                    existing.salary_disclosed = current.salary_disclosed
                    existing.salary_basis = current.salary_basis
                    existing.salary_text = current.salary_text
                    existing.search_query = current.search_query
                    existing.search_location = current.search_location
                    existing.apply_failure_reason = current.apply_failure_reason
                    existing.data = _dump(current)
                    existing.updated_at = now
                return False
            job.created_at = datetime.fromisoformat(now)
            job.updated_at = datetime.fromisoformat(now)
            session.add(
                JobRow(
                    id=job.id,
                    trace_id=job.trace_id,
                    company=job.company,
                    title=job.title,
                    location=job.location,
                    location_normalized=job.location_normalized,
                    url=job.url,
                    status=job.status.value,
                    score=job.score,
                    dedupe_hash=job.dedupe_hash,
                    company_id=job.company_id,
                    external_apply_url=job.external_apply_url,
                    external_apply_source=job.external_apply_source,
                    has_restriction=job.has_restriction,
                    restriction_details=job.restriction_details,
                    has_applied=job.has_applied,
                    applied_at=job.applied_at.isoformat() if job.applied_at else None,
                    easy_apply=job.easy_apply,
                    easy_apply_source=job.easy_apply_source,
                    salary_disclosed=job.salary_disclosed,
                    salary_basis=job.salary_basis,
                    salary_text=job.salary_text,
                    search_query=job.search_query,
                    search_location=job.search_location,
                    apply_failure_reason=job.apply_failure_reason,
                    data=_dump(job),
                    created_at=now,
                    updated_at=now,
                )
            )
            return True

    def get_job(self, job_id: str) -> JobOpportunity | None:
        with self.session() as session:
            row = session.get(JobRow, job_id)
            return JobOpportunity.model_validate(json.loads(row.data)) if row else None

    def update_job(self, job: JobOpportunity) -> None:
        if job.company.strip() and not job.company_id:
            company = self.ensure_company(job.company)
            if company is not None:
                job.company_id = company.id
        job.updated_at = datetime.now()
        with self.session() as session:
            row = session.get(JobRow, job.id)
            if row is None:
                return
            row.status = job.status.value
            row.score = job.score
            row.trace_id = job.trace_id
            row.location_normalized = job.location_normalized
            row.company_id = job.company_id
            row.external_apply_url = job.external_apply_url
            row.external_apply_source = job.external_apply_source
            row.has_restriction = job.has_restriction
            row.restriction_details = job.restriction_details
            row.has_applied = job.has_applied
            row.applied_at = job.applied_at.isoformat() if job.applied_at else None
            row.easy_apply = job.easy_apply
            row.easy_apply_source = job.easy_apply_source
            row.salary_disclosed = job.salary_disclosed
            row.salary_basis = job.salary_basis
            row.salary_text = job.salary_text
            row.search_query = job.search_query
            row.search_location = job.search_location
            row.apply_failure_reason = job.apply_failure_reason
            row.data = _dump(job)
            row.updated_at = job.updated_at.isoformat()

    def list_jobs(
        self,
        status: JobStatus | list[JobStatus] | None = None,
        limit: int | None = None,
        trace_id: str | None = None,
    ) -> list[JobOpportunity]:
        stmt = select(JobRow).order_by(JobRow.created_at.desc())
        if status is not None:
            statuses = status if isinstance(status, list) else [status]
            stmt = stmt.where(JobRow.status.in_([s.value for s in statuses]))
        if trace_id:
            stmt = stmt.where(JobRow.trace_id == trace_id)
        if limit:
            stmt = stmt.limit(limit)
        with self.session() as session:
            return [
                JobOpportunity.model_validate(json.loads(row.data))
                for row in session.scalars(stmt).all()
            ]

    def query_review_jobs(
        self,
        *,
        bucket: str = "approval",
        query: str = "",
        sort: str = "newer",
        page: int = 1,
        limit: int = 10,
        automation_consumers: tuple[str, ...] = ("application", "apply_submit"),
    ) -> dict[str, Any]:
        """Consulta da PWA inteiramente no banco: texto, bucket, ordem e página.

        A paginação acontece antes da desserialização, então a quantidade de
        vagas carregadas na memória cresce com ``limit``, nunca com o total do
        banco. Os contadores são ``COUNT`` separados com o mesmo filtro de
        texto, o que mantém os números das abas coerentes com a busca.
        """
        buckets = ("approval", "submitted", "rejected", "errors", "all")
        bucket = bucket if bucket in buckets else "approval"
        sort = "older" if sort == "older" else "newer"
        page = max(1, int(page))
        limit = max(1, min(int(limit), 100))

        conditions: list[Any] = []
        term = " ".join((query or "").split())
        if term:
            # Busca literal: % e _ digitados pelo usuário não viram curinga.
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            # ``data`` contém descrição, resumo da análise, motivos e skills.
            conditions.append(
                or_(
                    JobRow.title.ilike(like, escape="\\"),
                    JobRow.company.ilike(like, escape="\\"),
                    JobRow.location.ilike(like, escape="\\"),
                    JobRow.data.ilike(like, escape="\\"),
                )
            )

        failed_event = exists().where(
            AgentEventRow.job_id == JobRow.id,
            AgentEventRow.status == "FAILED",
            AgentEventRow.consumer.in_(automation_consumers),
        )
        error_cond = or_(JobRow.status == JobStatus.FAILED.value, failed_event)
        sent_cond = or_(
            JobRow.status == JobStatus.SUBMITTED.value,
            JobRow.has_applied.is_(True),
        )
        # ``data`` é Text, mas o JSON é válido. Cada dialeto extrai o caminho
        # de um jeito diferente; o COALESCE evita NULL (SQL de 3 valores) e
        # mantém a regra idêntica à de review_app._bucket.
        if self.engine.dialect.name == "sqlite":
            decision = func.coalesce(
                func.json_extract(JobRow.data, "$.qualification.decision"), ""
            )
        else:
            decision = func.coalesce(
                cast(JobRow.data, JSON)["qualification", "decision"].as_string(), ""
            )
        rejected_cond = or_(
            JobRow.status.in_(
                [JobStatus.REJECTED_BY_FILTER.value, JobStatus.EXPIRED.value]
            ),
            decision.in_(["SKIP", "REJECT"]),
        )

        def bucket_condition(key: str) -> Any | None:
            # Mesma precedência de review_app._bucket: erro → enviado → recusado.
            if key == "errors":
                return error_cond
            if key == "submitted":
                return and_(~error_cond, sent_cond)
            if key == "rejected":
                return and_(~error_cond, ~sent_cond, rejected_cond)
            if key == "approval":
                return and_(~error_cond, ~sent_cond, ~rejected_cond)
            return None

        def count_for(key: str) -> int:
            stmt = select(func.count()).select_from(JobRow).where(*conditions)
            condition = bucket_condition(key)
            if condition is not None:
                stmt = stmt.where(condition)
            with self.session() as session:
                return int(session.scalar(stmt) or 0)

        counts = {key: count_for(key) for key in buckets}
        counts["all"] = count_for("all")
        total = counts[bucket]
        pages = (total + limit - 1) // limit

        stmt = select(JobRow).where(*conditions)
        condition = bucket_condition(bucket)
        if condition is not None:
            stmt = stmt.where(condition)
        order = JobRow.created_at.asc() if sort == "older" else JobRow.created_at.desc()
        stmt = stmt.order_by(order, JobRow.id).limit(limit).offset((page - 1) * limit)
        with self.session() as session:
            jobs = [
                JobOpportunity.model_validate(json.loads(row.data))
                for row in session.scalars(stmt).all()
            ]

        return {
            "items": jobs,
            "counts": counts,
            "bucket": bucket,
            "query": term,
            "sort": sort,
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
        }

    # ---- empresas ----
    @staticmethod
    def normalize_company(name: str) -> str:
        # Sufixos são removidos DEPOIS de minúsculas: " LLC" maiúsculo nunca
        # casaria com "bairesdev llc" e a empresa virava duas entidades.
        return " ".join((name or "").lower().replace(" llc", "").replace(" s/a", "").split())

    def upsert_company(self, company: Company) -> Company:
        company.name_normalized = company.name_normalized or self.normalize_company(company.name)
        if not company.name_normalized:
            return company
        with self.session() as session:
            existing = session.scalar(
                select(CompanyRow).where(
                    CompanyRow.name_normalized == company.name_normalized
                )
            )
            if existing:
                current = Company.model_validate(json.loads(existing.data))
                # Campos preenchidos explicitamente não são apagados por re-leitura.
                company.id = existing.id
                company.created_at = current.created_at
                if not company.has_account:
                    company.has_account = current.has_account
                if company.account_filled_at is None:
                    company.account_filled_at = current.account_filled_at
                if company.last_form_filled_at is None:
                    company.last_form_filled_at = current.last_form_filled_at
                if company.technical_test == "unknown":
                    company.technical_test = current.technical_test
                if not company.technical_test_notes:
                    company.technical_test_notes = current.technical_test_notes
                # Supressao temporaria e estado operacional: nunca e apagado
                # por uma releitura da vaga na busca publica.
                if company.suppressed_until is None:
                    company.suppressed_until = current.suppressed_until
                if not company.suppression_reason:
                    company.suppression_reason = current.suppression_reason
                if company.suppression_checked_at is None:
                    company.suppression_checked_at = current.suppression_checked_at
                if not company.suppression_history:
                    company.suppression_history = current.suppression_history
                if not company.is_trusted_portal:
                    company.is_trusted_portal = current.is_trusted_portal
                if not company.trusted_portal_config:
                    company.trusted_portal_config = current.trusted_portal_config
                existing.data = _dump(company)
                existing.has_account = company.has_account
                existing.technical_test = company.technical_test
                existing.is_trusted_portal = company.is_trusted_portal
                existing.trusted_portal_config = json.dumps(
                    company.trusted_portal_config, ensure_ascii=False
                )
                existing.updated_at = company.updated_at.isoformat()
                return company
            session.add(
                CompanyRow(
                    id=company.id,
                    name=company.name,
                    name_normalized=company.name_normalized,
                    has_account=company.has_account,
                    technical_test=company.technical_test,
                    is_trusted_portal=company.is_trusted_portal,
                    trusted_portal_config=json.dumps(
                        company.trusted_portal_config, ensure_ascii=False
                    ),
                    data=_dump(company),
                    created_at=company.created_at.isoformat(),
                    updated_at=company.updated_at.isoformat(),
                )
            )
            return company

    def mark_company_application(self, company_id: str, *, created_account: bool) -> Company | None:
        """Registra que a empresa tem cadastro e que um formulário foi preenchido.

        ``created_account`` só é True quando o fluxo terminou em cadastro novo;
        em candidaturas com login existente apenas ``last_form_filled_at`` muda.
        """
        if not company_id:
            return None
        with self.session() as session:
            row = session.get(CompanyRow, company_id)
            if row is None:
                return None
            company = Company.model_validate(json.loads(row.data))
            now = datetime.now()
            company.has_account = True
            company.last_form_filled_at = now
            if created_account and company.account_filled_at is None:
                company.account_filled_at = now
            company.updated_at = now
            row.data = _dump(company)
            row.has_account = True
            row.updated_at = now.isoformat()
            return company

    def ensure_company(self, name: str) -> Company | None:
        """Busca ou cria a empresa para vincular o ``company_id`` à vaga.

        Usada no momento da gravação da vaga. Preserva qualquer cadastro
        existente (inclusive supressão e dados preenchidos à mão).
        """
        name = (name or "").strip()
        if not name:
            return None
        company = self.get_company_by_name(name)
        if company is not None:
            return company
        normalized = self.normalize_company(name)
        has_account = any(
            portal in normalized or normalized in portal
            for portal in self.known_portals
        )
        return self.upsert_company(
            Company(name=name, has_account=has_account, technical_test="unknown")
        )

    def get_company_by_name(self, name: str) -> Company | None:
        normalized = self.normalize_company(name)
        with self.session() as session:
            row = session.scalar(
                select(CompanyRow).where(CompanyRow.name_normalized == normalized)
            )
            return Company.model_validate(json.loads(row.data)) if row else None

    def list_companies(self) -> list[Company]:
        with self.session() as session:
            return [
                Company.model_validate(json.loads(row.data))
                for row in session.scalars(select(CompanyRow).order_by(CompanyRow.name)).all()
            ]

    # ---- portais confiáveis (fast lane) ----
    def list_trusted_portals(self) -> list[Company]:
        with self.session() as session:
            rows = session.scalars(
                select(CompanyRow)
                .where(CompanyRow.is_trusted_portal.is_(True))
                .order_by(CompanyRow.name)
            ).all()
            return [Company.model_validate(json.loads(row.data)) for row in rows]

    def set_trusted_portal(
        self, name: str, config: dict[str, Any], *, overwrite: bool = True
    ) -> Company | None:
        """Marca a empresa como portal confiável.

        ``overwrite=False`` é usado pelo seed: nunca sobrescreve ajustes manuais.
        """
        company = self.get_company_by_name(name)
        if company is None:
            return None
        if not overwrite and company.is_trusted_portal and company.trusted_portal_config:
            return company
        company.is_trusted_portal = True
        company.trusted_portal_config = config
        return self.upsert_company(company)

    # ---- watermark do discovery ----
    def get_discovery_watermark(self, slot: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.get(DiscoveryWatermarkRow, slot)
            return json.loads(row.data) if row else None

    def save_discovery_watermark(self, slot: str, data: dict[str, Any]) -> None:
        with self.session() as session:
            row = session.get(DiscoveryWatermarkRow, slot)
            payload = json.dumps(data, ensure_ascii=False, default=str)
            now = datetime.now().isoformat()
            if row is None:
                session.add(DiscoveryWatermarkRow(slot=slot, data=payload, updated_at=now))
            else:
                row.data = payload
                row.updated_at = now

    def is_company_suppressed(self, name: str) -> bool:
        """True se a empresa esta em periodo de supressao temporaria.

        O filtro e aplicado ANTES de salvar a vaga na busca publica — nao
        altera a busca em si, apenas evita persistir vagas de empresas cuja
        restricao geografica ja foi comprovada. Expira automaticamente.
        """
        company = self.get_company_by_name(name)
        if company is None or company.suppressed_until is None:
            return False
        return company.suppressed_until > datetime.now()

    def suppress_company(
        self, name: str, *, days: int = 30, reason: str = ""
    ) -> Company:
        """Marca a empresa para ser ignorada por ``days`` dias ao salvar vagas."""
        company = self.get_company_by_name(name) or Company(
            name=name, name_normalized=self.normalize_company(name)
        )
        agora = datetime.now()
        company.suppressed_until = agora + timedelta(days=days)
        company.suppression_reason = reason or company.suppression_reason
        company.suppression_checked_at = agora
        company.suppression_history.append(
            f"{agora.isoformat(timespec='seconds')}: {days}d — {reason}"
        )
        return self.upsert_company(company)

    def clear_company_suppression(self, name: str) -> Company | None:
        """Remove a supressao (usado apos o periodo expirar ou revisao manual)."""
        company = self.get_company_by_name(name)
        if company is None:
            return None
        company.suppressed_until = None
        company.suppression_reason = ""
        return self.upsert_company(company)

    def link_company_to_job(self, job_id: str, company_id: str) -> None:
        with self.session() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                return
            data = json.loads(row.data)
            data["company_id"] = company_id
            row.company_id = company_id
            row.data = json.dumps(data, ensure_ascii=False)
            row.updated_at = datetime.now().isoformat()

    def seed_known_companies(self) -> int:
        """BairesDev/Strider: cadastros antigos, sem data; testes conforme informado."""
        entries = [
            Company(
                name="BairesDev",
                has_account=True,
                account_filled_at=None,
                last_form_filled_at=None,
                technical_test="known",
                technical_test_notes=(
                    "Testes técnicos de quiz já realizados para vagas da BairesDev; "
                    "não refazer."
                ),
            ),
            Company(
                name="Strider",
                has_account=True,
                account_filled_at=None,
                last_form_filled_at=None,
                technical_test="none",
                technical_test_notes=(
                    "Sem teste técnico na Strider; o cliente final pode exigir teste."
                ),
            ),
        ]
        added = 0
        for company in entries:
            if self.get_company_by_name(company.name) is None:
                self.upsert_company(company)
                added += 1
        # Flag de portal confiável + filtros simples, aplicados de forma
        # idempotente. Se o operador já ajustou o JSON, ele é preservado.
        for name, config in TRUSTED_PORTALS.items():
            self.set_trusted_portal(name, config, overwrite=False)
        return added

    # ---- dependências ----
    def add_dependency(self, dependency: ApplicationDependency) -> bool:
        """Retorna False se já existe a mesma dependência para a vaga."""
        with self.session() as session:
            existing = session.scalar(
                select(DependencyRow).where(
                    DependencyRow.opportunity_id == dependency.opportunity_id,
                    DependencyRow.kind == dependency.kind,
                )
            )
            if existing:
                return False
            session.add(
                DependencyRow(
                    id=dependency.id,
                    trace_id=dependency.trace_id,
                    opportunity_id=dependency.opportunity_id,
                    application_id=dependency.application_id,
                    kind=dependency.kind,
                    owner=dependency.owner,
                    status=dependency.status,
                    data=_dump(dependency),
                    created_at=dependency.created_at.isoformat(),
                    updated_at=dependency.updated_at.isoformat(),
                )
            )
            return True

    def update_dependency(self, dependency: ApplicationDependency) -> None:
        dependency.updated_at = datetime.now()
        with self.session() as session:
            row = session.get(DependencyRow, dependency.id)
            if row is None:
                return
            row.status = dependency.status
            row.application_id = dependency.application_id
            row.data = _dump(dependency)
            row.updated_at = dependency.updated_at.isoformat()

    def list_dependencies(
        self,
        opportunity_id: str | None = None,
        owner: str | None = None,
        status: str | None = None,
        trace_id: str | None = None,
        kind: str | None = None,
    ) -> list[ApplicationDependency]:
        stmt = select(DependencyRow)
        if opportunity_id:
            stmt = stmt.where(DependencyRow.opportunity_id == opportunity_id)
        if kind:
            stmt = stmt.where(DependencyRow.kind == kind)
        if trace_id:
            stmt = stmt.where(DependencyRow.trace_id == trace_id)
        if owner:
            stmt = stmt.where(DependencyRow.owner == owner)
        if status:
            stmt = stmt.where(DependencyRow.status == status)
        with self.session() as session:
            return [
                ApplicationDependency.model_validate(json.loads(row.data))
                for row in session.scalars(stmt.order_by(DependencyRow.created_at)).all()
            ]

    # ---- candidaturas ----
    def has_application_for(self, opportunity_id: str) -> bool:
        with self.session() as session:
            return (
                session.scalar(
                    select(ApplicationRow).where(
                        ApplicationRow.opportunity_id == opportunity_id
                    )
                )
                is not None
            )

    def record_application(self, application: Application) -> None:
        with self.session() as session:
            session.add(
                ApplicationRow(
                    id=application.id,
                    trace_id=application.trace_id,
                    opportunity_id=application.opportunity_id,
                    company=application.company,
                    title=application.title,
                    status=application.status,
                    applied_at=application.applied_at.isoformat(),
                    data=_dump(application),
                )
            )

    def list_applications(
        self, limit: int = 100, trace_id: str | None = None
    ) -> list[Application]:
        stmt = select(ApplicationRow).order_by(ApplicationRow.applied_at.desc()).limit(limit)
        if trace_id:
            stmt = stmt.where(ApplicationRow.trace_id == trace_id)
        with self.session() as session:
            rows = session.scalars(stmt).all()
            return [Application.model_validate(json.loads(row.data)) for row in rows]

    # ---- limites diários ----
    def get_daily_limit(self, day: str | None = None, limit: int = 10) -> DailyLimit:
        day = day or date.today().isoformat()
        with self.session() as session:
            row = session.get(DailyLimitRow, day)
            if row is None:
                row = DailyLimitRow(date=day, submitted=0, limit=limit)
                session.add(row)
            return DailyLimit(date=row.date, submitted=row.submitted, limit=row.limit)

    def reserve_daily_slot(self, limit: int, day: str | None = None) -> bool:
        """Incrementa o contador somente se ainda houver saldo (operação atômica)."""
        day = day or date.today().isoformat()
        with self.session() as session:
            row = session.get(DailyLimitRow, day)
            if row is None:
                row = DailyLimitRow(date=day, submitted=0, limit=limit)
                session.add(row)
                session.flush()
            result = session.execute(
                update(DailyLimitRow)
                .where(
                    DailyLimitRow.date == day,
                    DailyLimitRow.submitted < DailyLimitRow.limit,
                )
                .values(submitted=DailyLimitRow.submitted + 1)
            )
            return result.rowcount == 1

    def release_daily_slot(self, day: str | None = None) -> bool:
        """Devolve uma reserva quando a candidatura não foi enviada."""
        day = day or date.today().isoformat()
        with self.session() as session:
            row = session.get(DailyLimitRow, day)
            if row is None or row.submitted <= 0:
                return False
            row.submitted -= 1
            return True

    # ---- travamento de execução (uma busca por vez) ----
    def try_acquire_run_lock(
        self,
        name: str,
        owner: str,
        *,
        stale_after_seconds: int,
        now: datetime | None = None,
    ) -> tuple[bool, bool, str | None]:
        """Tenta tomar a trava. Retorna ``(adquiriu, recuperou_stale, dono_anterior)``.

        A escrita é atômica em SQLite e Postgres: insere a linha ou substitui
        apenas uma trava cujo heartbeat parou há ``stale_after_seconds``.
        """
        current = now or datetime.now(timezone.utc)
        current_iso = current.isoformat()
        stale_iso = (current - timedelta(seconds=stale_after_seconds)).isoformat()
        statement = text(
            "INSERT INTO run_locks (name, owner, acquired_at, renewed_at) "
            "VALUES (:name, :owner, :acquired, :renewed) "
            "ON CONFLICT(name) DO UPDATE SET "
            "owner=excluded.owner, acquired_at=excluded.acquired_at, "
            "renewed_at=excluded.renewed_at "
            "WHERE run_locks.renewed_at < :stale"
        )
        with self.Session() as session:
            with session.begin():
                existing = session.get(RunLockRow, name)
                previous_owner = existing.owner if existing is not None else None
                result = session.execute(
                    statement,
                    {
                        "name": name,
                        "owner": owner,
                        "acquired": current_iso,
                        "renewed": current_iso,
                        "stale": stale_iso,
                    },
                )
                acquired = result.rowcount > 0
                reclaimed = acquired and previous_owner not in (None, owner)
                return acquired, reclaimed, previous_owner

    def renew_run_lock(
        self, name: str, owner: str, *, now: datetime | None = None
    ) -> bool:
        """Renova o heartbeat somente se esta instância ainda for a dona."""
        current = (now or datetime.now(timezone.utc)).isoformat()
        statement = (
            update(RunLockRow)
            .where(RunLockRow.name == name, RunLockRow.owner == owner)
            .values(renewed_at=current)
        )
        with self.Session() as session:
            with session.begin():
                return session.execute(statement).rowcount > 0

    def release_run_lock(self, name: str, owner: str) -> bool:
        statement = text("DELETE FROM run_locks WHERE name = :name AND owner = :owner")
        with self.Session() as session:
            with session.begin():
                return session.execute(statement, {"name": name, "owner": owner}).rowcount > 0

    def get_run_lock(self, name: str) -> dict[str, str] | None:
        with self.session() as session:
            row = session.get(RunLockRow, name)
            if row is None:
                return None
            return {
                "name": row.name,
                "owner": row.owner,
                "acquired_at": row.acquired_at,
                "renewed_at": row.renewed_at,
            }

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def mark_orphan_executions(self, cutoff: datetime) -> int:
        """Marca rodadas RUNNING antigas como ABANDONED após reclaim da trava."""
        limit = self._aware(cutoff)
        changed = 0
        with self.session() as session:
            rows = session.scalars(
                select(ExecutionRow).where(
                    ExecutionRow.status == ExecutionStatus.RUNNING.value
                )
            ).all()
            for row in rows:
                try:
                    started = self._aware(datetime.fromisoformat(row.started_at))
                except (TypeError, ValueError):
                    continue
                if started >= limit:
                    continue
                data = json.loads(row.data)
                data["status"] = ExecutionStatus.ABANDONED.value
                row.status = ExecutionStatus.ABANDONED.value
                row.data = json.dumps(data, ensure_ascii=False, default=str)
                changed += 1
        return changed

    def mark_orphan_watermarks(self, cutoff: datetime) -> int:
        """Same cleanup para watermarks RUNNING deixados por restart."""
        limit = self._aware(cutoff)
        changed = 0
        with self.session() as session:
            rows = session.scalars(select(DiscoveryWatermarkRow)).all()
            for row in rows:
                data = json.loads(row.data)
                if data.get("status") != ExecutionStatus.RUNNING.value:
                    continue
                try:
                    started = self._aware(datetime.fromisoformat(str(data["started_at"])))
                except (KeyError, TypeError, ValueError):
                    continue
                if started >= limit:
                    continue
                data["status"] = ExecutionStatus.ABANDONED.value
                row.data = json.dumps(data, ensure_ascii=False, default=str)
                row.updated_at = datetime.now().isoformat()
                changed += 1
        return changed

    # ---- execuções e checkpoint ----
    def create_execution(self, history: ExecutionHistory) -> None:
        with self.session() as session:
            session.add(
                ExecutionRow(
                    id=history.id,
                    trace_id=history.trace_id or history.id,
                    started_at=history.started_at.isoformat(),
                    status=history.status,
                    data=_dump(history),
                )
            )

    def update_execution(self, history: ExecutionHistory) -> None:
        history.trace_id = history.trace_id or history.id
        with self.session() as session:
            row = session.get(ExecutionRow, history.id)
            if row:
                row.status = history.status
                row.trace_id = history.trace_id
                row.data = _dump(history)

    def list_executions(self, limit: int = 20) -> list[ExecutionHistory]:
        with self.session() as session:
            rows = session.scalars(
                select(ExecutionRow).order_by(ExecutionRow.started_at.desc()).limit(limit)
            ).all()
            return [ExecutionHistory.model_validate(json.loads(row.data)) for row in rows]

    def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        with self.session() as session:
            row = session.get(CheckpointRow, run_id)
            payload = json.dumps(state, ensure_ascii=False, default=str)
            if row is None:
                session.add(CheckpointRow(run_id=run_id, data=payload, updated_at=now))
            else:
                row.data = payload
                row.updated_at = now

    def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.get(CheckpointRow, run_id)
            return json.loads(row.data) if row else None

    # ---- estado de notificações periódicas ----
    def last_notification_date(self, key: str) -> date | None:
        with self.session() as session:
            row = session.get(NotificationStateRow, key)
            if row is None:
                return None
            try:
                return date.fromisoformat(row.value)
            except ValueError:
                return None

    def mark_notification_sent(self, key: str, when: datetime | None = None) -> None:
        """Registra o dia do envio; chamado somente após sucesso do Gmail."""
        when = when or datetime.now()
        day = when.date().isoformat()
        with self.session() as session:
            row = session.get(NotificationStateRow, key)
            if row is None:
                session.add(
                    NotificationStateRow(key=key, value=day, updated_at=when.isoformat())
                )
            else:
                row.value = day
                row.updated_at = when.isoformat()

    def record_notification(
        self,
        kind: str,
        subject: str,
        message_id: str,
        trace_ids: list[str],
        sent_at: datetime | None = None,
    ) -> None:
        """Guarda o envio para rastrear a evolução dos e-mails por execução."""
        sent_at = sent_at or datetime.now()
        with self.session() as session:
            session.add(
                NotificationLogRow(
                    id=str(uuid4()),
                    kind=kind,
                    trace_ids=",".join(dict.fromkeys(t for t in trace_ids if t)),
                    subject=subject,
                    message_id=message_id,
                    sent_at=sent_at.isoformat(),
                )
            )

    def list_notifications(
        self, limit: int = 50, trace_id: str | None = None
    ) -> list[dict[str, str]]:
        stmt = select(NotificationLogRow).order_by(NotificationLogRow.sent_at.desc()).limit(limit)
        with self.session() as session:
            rows = session.scalars(stmt).all()
            result = [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "trace_ids": row.trace_ids,
                    "subject": row.subject,
                    "message_id": row.message_id,
                    "sent_at": row.sent_at,
                }
                for row in rows
            ]
        if trace_id:
            result = [row for row in result if trace_id in row["trace_ids"].split(",")]
        return result

    def get_execution_by_trace(self, trace_id: str) -> ExecutionHistory | None:
        with self.session() as session:
            row = session.scalar(
                select(ExecutionRow).where(ExecutionRow.trace_id == trace_id)
            )
            return ExecutionHistory.model_validate(json.loads(row.data)) if row else None

    # ---- aprovações ----
    def create_approval(self, approval: ApprovalRequest) -> None:
        with self.session() as session:
            session.add(
                ApprovalRow(
                    id=approval.id,
                    trace_id=approval.trace_id,
                    opportunity_id=approval.opportunity_id,
                    status=approval.status,
                    data=_dump(approval),
                    created_at=approval.created_at.isoformat(),
                )
            )

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        with self.session() as session:
            row = session.get(ApprovalRow, approval_id)
            return ApprovalRequest.model_validate(json.loads(row.data)) if row else None

    def resolve_approval(self, approval_id: str, approved: bool) -> ApprovalRequest | None:
        with self.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                return None
            data = json.loads(row.data)
            data["status"] = "APPROVED" if approved else "REJECTED"
            data["resolved_at"] = datetime.now().isoformat()
            row.status = data["status"]
            row.data = json.dumps(data, ensure_ascii=False)
            return ApprovalRequest.model_validate(data)

    def list_pending_approvals(self) -> list[ApprovalRequest]:
        with self.session() as session:
            rows = session.scalars(
                select(ApprovalRow).where(ApprovalRow.status == "PENDING")
            ).all()
            return [ApprovalRequest.model_validate(json.loads(row.data)) for row in rows]

    def list_approvals(self) -> list[ApprovalRequest]:
        """Todas as aprovações, para classes rápidas sem N+1 consultas."""
        with self.session() as session:
            rows = session.scalars(select(ApprovalRow)).all()
            return [ApprovalRequest.model_validate(json.loads(row.data)) for row in rows]

    def failed_agent_events(
        self,
        consumers: tuple[str, ...] | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Eventos de automação encerrados com FAILED (com o motivo do erro)."""
        stmt = select(AgentEventRow).where(AgentEventRow.status == "FAILED")
        if consumers:
            stmt = stmt.where(AgentEventRow.consumer.in_(consumers))
        if job_id:
            stmt = stmt.where(AgentEventRow.job_id == job_id)
        with self.session() as session:
            return [
                {
                    "job_id": row.job_id,
                    "consumer": row.consumer,
                    "event_type": row.event_type,
                    "error": row.error,
                    "updated_at": row.updated_at,
                }
                for row in session.scalars(stmt.order_by(AgentEventRow.updated_at.desc())).all()
            ]

    def get_approval_for_job(self, opportunity_id: str) -> ApprovalRequest | None:
        """Última aprovação registrada para uma vaga (a mais recente primeiro)."""
        with self.session() as session:
            row = session.scalar(
                select(ApprovalRow)
                .where(ApprovalRow.opportunity_id == opportunity_id)
                .order_by(ApprovalRow.created_at.desc())
            )
            return ApprovalRequest.model_validate(json.loads(row.data)) if row else None

    # ---- decisões da plataforma intermediária ----
    def record_review_decision(
        self,
        opportunity_id: str,
        decision: str,
        *,
        approval_id: str | None = None,
        reviewer: str = "operator",
        checks: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Registra Aprovar/Rejeitar com auditoria e devolve o registro."""
        payload = {
            "id": str(uuid4()),
            "opportunity_id": opportunity_id,
            "approval_id": approval_id,
            "decision": decision,
            "reviewer": reviewer,
            "checks": checks or {},
            "note": note,
            "created_at": datetime.now().isoformat(),
        }
        with self.session() as session:
            session.add(
                ReviewDecisionRow(
                    id=payload["id"],
                    opportunity_id=opportunity_id,
                    approval_id=approval_id,
                    decision=decision,
                    reviewer=reviewer,
                    checks=json.dumps(payload["checks"], ensure_ascii=False),
                    note=note,
                    created_at=payload["created_at"],
                )
            )
        return payload

    def get_review_decision_for_job(self, opportunity_id: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.scalar(
                select(ReviewDecisionRow)
                .where(ReviewDecisionRow.opportunity_id == opportunity_id)
                .order_by(ReviewDecisionRow.created_at.desc())
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "opportunity_id": row.opportunity_id,
                "approval_id": row.approval_id,
                "decision": row.decision,
                "reviewer": row.reviewer,
                "checks": json.loads(row.checks or "{}"),
                "note": row.note,
                "created_at": row.created_at,
            }

    # ---- outbox de eventos entre workers ----
    @staticmethod
    def agent_event_id(consumer: str, event_type: str, job_id: str) -> str:
        """Chave estável: o mesmo aviso nunca é duplicado na fila."""
        return f"{consumer}|{event_type}|{job_id}"

    def publish_agent_event(
        self,
        consumer: str,
        event_type: str,
        job_id: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Publica um aviso persistente. Retorna False se já havia um pendente.

        Um evento encerrado (``ACK``/``FAILED``) é reativado para ``PENDING``:
        se a vaga for reclassificada, o consumidor deve ser acordado de novo.
        """
        event_id = self.agent_event_id(consumer, event_type, job_id)
        now = datetime.now().isoformat()
        data = json.dumps(payload or {}, ensure_ascii=False)
        try:
            with self.session() as session:
                row = session.get(AgentEventRow, event_id)
                if row is None:
                    session.add(
                        AgentEventRow(
                            id=event_id,
                            consumer=consumer,
                            event_type=event_type,
                            job_id=job_id,
                            status="PENDING",
                            payload=data,
                            error="",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    return True
                if row.status in {"ACK", "FAILED"}:
                    row.status = "PENDING"
                    row.payload = data
                    row.error = ""
                    row.updated_at = now
                    return True
                return False
        except IntegrityError:
            # Outro publicador ganhou a corrida; o aviso já está na fila.
            return False

    def claim_agent_events(
        self, consumer: str, limit: int = 20, *, stale_after_seconds: int = 900
    ) -> list[dict[str, Any]]:
        """Reserva eventos para processamento e devolve o payload já decodificado.

        Eventos ``PROCESSING`` órfãos de um restart são recuperados depois de
        ``stale_after_seconds``. No Postgres usa ``FOR UPDATE SKIP LOCKED`` para
        que dois consumidores não peguem o mesmo aviso.
        """
        now = datetime.now()
        stale = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        stmt = (
            select(AgentEventRow)
            .where(
                AgentEventRow.consumer == consumer,
                (AgentEventRow.status == "PENDING")
                | (
                    (AgentEventRow.status == "PROCESSING")
                    & (AgentEventRow.updated_at < stale)
                ),
            )
            .order_by(AgentEventRow.created_at, AgentEventRow.id)
            .limit(limit)
        )
        if self.engine.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        with self.session() as session:
            rows = session.scalars(stmt).all()
            if not rows:
                return []
            for row in rows:
                row.status = "PROCESSING"
                row.updated_at = now.isoformat()
            return [
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "job_id": row.job_id,
                    "payload": json.loads(row.payload or "{}"),
                }
                for row in rows
            ]

    def ack_agent_event(self, event_id: str, *, status: str = "ACK", error: str = "") -> bool:
        """Encerra um evento. ``FAILED`` guarda o motivo para auditoria."""
        with self.session() as session:
            row = session.get(AgentEventRow, event_id)
            if row is None:
                return False
            row.status = status if status in {"ACK", "FAILED"} else "ACK"
            row.error = error[:2000]
            row.updated_at = datetime.now().isoformat()
            return True

    # ---- memória de classificação ----
    def record_qualification_feedback(self, feedback: QualificationFeedback) -> bool:
        """Grava um sinal de aprendizado. Retorna False se já era conhecido."""
        with self.session() as session:
            duplicata = session.scalar(
                select(QualificationFeedbackRow).where(
                    QualificationFeedbackRow.opportunity_id == feedback.opportunity_id,
                    QualificationFeedbackRow.source == feedback.source,
                    QualificationFeedbackRow.verdict == feedback.verdict,
                    QualificationFeedbackRow.topic == feedback.topic,
                )
            )
            if duplicata is not None:
                return False
            session.add(
                QualificationFeedbackRow(
                    id=feedback.id,
                    trace_id=feedback.trace_id,
                    opportunity_id=feedback.opportunity_id,
                    source=feedback.source,
                    verdict=feedback.verdict,
                    topic=feedback.topic,
                    data=_dump(feedback),
                    created_at=feedback.created_at.isoformat(),
                )
            )
            return True

    def list_qualification_feedback(
        self, *, topic: str = "", limit: int = 200
    ) -> list[QualificationFeedback]:
        with self.session() as session:
            statement = select(QualificationFeedbackRow)
            if topic:
                statement = statement.where(QualificationFeedbackRow.topic == topic)
            rows = session.scalars(
                statement.order_by(QualificationFeedbackRow.created_at.desc()).limit(limit)
            ).all()
            return [QualificationFeedback.model_validate(json.loads(row.data)) for row in rows]

    # ---- manutenção ----
    def expire_stale_jobs(
        self,
        cutoff: datetime,
        keep_statuses: set[str] | None = None,
        apply: bool = True,
    ) -> list[str]:
        """Marca como EXPIRED vagas fora da janela — NUNCA exclui.

        O histórico é preservado para medição temporal (candidaturas x retornos).
        Vagas em candidatura/aprovação não são tocadas. Com ``apply=False``
        apenas retorna os IDs que seriam marcados.
        """
        keep = keep_statuses or {
            JobStatus.SUBMITTED.value,
            JobStatus.FAILED.value,
            JobStatus.REQUIRES_HUMAN_ACTION.value,
            JobStatus.PENDING_APPROVAL.value,
            JobStatus.WAITING_DEVICE.value,
            JobStatus.APPLYING.value,
            JobStatus.EXPIRED.value,
        }
        stale = [
            job.id
            for job in self.list_jobs()
            if job.status.value not in keep
            and job.status != JobStatus.EXPIRED
            and (job.posted_at is None or job.posted_at < cutoff)
        ]
        if stale and apply:
            with self.session() as session:
                for job_id in stale:
                    row = session.get(JobRow, job_id)
                    if row is None:
                        continue
                    data = json.loads(row.data)
                    data["status"] = JobStatus.EXPIRED.value
                    row.status = JobStatus.EXPIRED.value
                    row.data = json.dumps(data, ensure_ascii=False)
        return stale

    # Alias legado — mantém compatibilidade, mas agora só marca histórico.
    def prune_stale_jobs(self, *args, **kwargs) -> list[str]:
        return self.expire_stale_jobs(*args, **kwargs)

    # ---- relatório ----
    def stats(self) -> dict[str, int]:
        with self.session() as session:
            statuses = session.scalars(select(JobRow.status)).all()
            return {
                "jobs_total": len(statuses),
                "jobs_discovered": statuses.count(JobStatus.DISCOVERED.value),
                "jobs_qualified": statuses.count(JobStatus.QUALIFIED.value),
                "jobs_waiting_device": statuses.count(JobStatus.WAITING_DEVICE.value),
                "jobs_submitted": statuses.count(JobStatus.SUBMITTED.value),
                "applications": len(session.scalars(select(ApplicationRow.id)).all()),
                "pending_approvals": len(
                    session.scalars(
                        select(ApprovalRow.id).where(ApprovalRow.status == "PENDING")
                    ).all()
                ),
            }
