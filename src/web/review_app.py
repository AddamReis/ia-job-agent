"""PWA de revisão das classificações e do check de autorização do Apply.

Servidor HTTP da própria stdlib: nenhuma dependência nova é necessária na DGX.
Os dados vêm do mesmo Postgres do agente. O Tailscale Serve publica este
servidor em HTTPS dentro da tailnet, o que permite instalá-lo como PWA.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..agents.qualification import (
    detect_currencies,
    explicit_currency_codes,
    is_remote_location,
    currency_rejection_reason,
    stale_reason,
)
from ..core.config import Settings
from ..core.state import JobStatus
from ..models.domain import JobOpportunity
from ..storage.database import Repository
from ..storage.evidence_store import (
    EVIDENCE_SUFFIXES,
    cleanup_expired,
    file_is_expired,
    is_expired,
    retention_cutoff,
)

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_CURRENCIES = {"USD", "EUR"}
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024

# Ordem fixa da interface. ``all`` é apenas filtro, nunca classificação de vaga.
BUCKETS: tuple[tuple[str, str], ...] = (
    ("approval", "Aguardando Aprovação"),
    ("pending", "A Classificar"),
    ("submitted", "Enviados"),
    ("rejected", "Recusado"),
    ("errors", "Erros"),
    ("all", "Todas"),
)
BUCKET_KEYS = tuple(key for key, _ in BUCKETS)
DEFAULT_BUCKET = BUCKETS[0][0]
PAGE_SIZE = 10
# Consumidores cuja falha representa erro de automação, não decisão humana.
AUTOMATION_CONSUMERS = ("application", "apply_submit")


def _settings() -> Settings:
    return Settings.load()


def _repo() -> Repository:
    return Repository()


def _evidence_root() -> Path:
    return _settings().resolve("data/evidence")


def resolve_evidence_path(job_id: str, filename: str) -> Path | None:
    """Valida o caminho da evidência e bloqueia travessia/symlink."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or ""):
        return None
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", filename or "")
        or Path(filename).suffix.lower() not in EVIDENCE_SUFFIXES
    ):
        return None
    base = _evidence_root().resolve()
    alvo = (base / job_id / filename).resolve()
    if base not in alvo.parents or not alvo.is_file():
        return None
    if alvo.stat().st_size > EVIDENCE_MAX_BYTES:
        return None
    return alvo


def _fresh_evidence(
    job_id: str, itens: list[dict[str, Any]], settings: Settings
) -> list[dict[str, Any]]:
    """Limpeza lazy: expira metadados/arquivos fora da janela de retenção."""
    dias = settings.application.evidence_retention_days
    cutoff = retention_cutoff(dias)
    if dias <= 0:
        return itens
    cleanup_expired(
        settings.resolve("data/evidence"), retention_days=dias, job_id=job_id
    )
    return [item for item in itens if not is_expired(item, cutoff=cutoff, retention_days=dias)]


def _serialize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(v) for v in value]
    return value


def _bucket(
    job: JobOpportunity,
    min_score: int,
    *,
    approval_status: str | None = None,
    automation_error: str = "",
) -> str:
    """Classifica a vaga em uma das pastas da plataforma intermediária.

    A ordem de precedência é erro → enviado → recusado → aguardando aprovação.
    ``min_score`` permanece no contrato por compatibilidade; a reclassificação
    determinística já grava o status ``REJECTED_BY_FILTER`` quando o score baixa.
    """
    del min_score, approval_status
    if automation_error or job.status == JobStatus.FAILED:
        return "errors"
    if job.status == JobStatus.SUBMITTED or job.has_applied:
        return "submitted"
    if job.status in {JobStatus.REJECTED_BY_FILTER, JobStatus.EXPIRED} or (
        job.qualification is not None and job.qualification.decision in {"SKIP", "REJECT"}
    ):
        return "rejected"
    # Ainda sem classificação tem pasta própria: a soma das abas fecha com o
    # total e a fila de aprovação não é inflada enquanto o classificador atua.
    if job.status in {JobStatus.DISCOVERED, JobStatus.ANALYZED}:
        return "pending"
    # Inclui QUALIFIED/PENDING_APPROVAL (dependem do check), WAITING_DEVICE e
    # REQUIRES_HUMAN_ACTION (dependem de ação humana) e APPLYING (em andamento).
    return "approval"


# Tecnologias de core que merecem badge quando faltam. Plataformas
# (Docker/Jenkins/Kafka/AWS) e diferenciais ficam de fora de propósito: as
# regras de fit já não descontam por eles.
_CORE_TECH_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"next\.?js|nextjs", "Next.js"),
    (r"\bjava\b(?!script)", "Java"),
    (r"\bjavascript\b", "JavaScript"),
    (r"\btypescript\b", "TypeScript"),
    (r"\bnode\.?js\b|\bnodejs\b", "Node.js"),
    (r"\breact(?:\.js)?\b", "React"),
    (r"\bangular\b", "Angular"),
    (r"\bvue(?:\.js)?\b", "Vue"),
    (r"\bpython\b", "Python"),
    (r"\bphp\b", "PHP"),
    (r"\bruby\b|\brails\b", "Ruby"),
    (r"\bgolang\b|\bgo\s+lang\b", "Go"),
    (r"\bkotlin\b", "Kotlin"),
    (r"\bswift\b", "Swift"),
    (r"\brust\b", "Rust"),
    (r"\bscala\b", "Scala"),
    (r"(?<!\w)\.net\b|\bdotnet\b|\basp\.net\b", ".NET"),
    (r"(?<!\w)c#(?!\w)|\bcsharp\b", "C#"),
    (r"\bdjango\b", "Django"),
    (r"\bflask\b", "Flask"),
    (r"\bspring(?:\s+boot)?\b", "Spring"),
    (r"\blaravel\b", "Laravel"),
    (r"\bexpress(?:\.js)?\b", "Express"),
    (r"\bpostgres(?:ql)?\b", "Postgres"),
    (r"\bsql\s+server\b", "SQL Server"),
    (r"\bmysql\b", "MySQL"),
    (r"\bmongo\s?db\b", "MongoDB"),
    (r"\boracle\b", "Oracle"),
)


def missing_core_technologies(job: JobOpportunity, limit: int = 3) -> list[str]:
    """Tecnologias core da vaga que o perfil não cobre, para badge na PWA.

    Só entram os vereditos ``core_missing`` do analista de fit. Requisitos não
    tecnológicos e plataformas são descartados pelo filtro de padrões, evitando
    badge de "falta Docker" ou "faltam 5 anos de experiência".
    """
    fit = job.fit_assessment
    if fit is None:
        return []
    text_parts: list[str] = []
    for verdict in fit.requirement_verdicts:
        if getattr(verdict, "verdict", None) != "core_missing":
            continue
        text_parts.append(str(getattr(verdict, "requirement", "") or ""))
        text_parts.append(str(getattr(verdict, "note", "") or ""))
    texto = " ".join(text_parts).lower()
    if not texto.strip():
        return []
    faltando: list[str] = []
    for pattern, label in _CORE_TECH_PATTERNS:
        if label in faltando:
            continue
        if re.search(pattern, texto, re.I):
            faltando.append(label)
            if len(faltando) >= limit:
                break
    return faltando


def _summary_job(
    job: JobOpportunity,
    min_score: int,
    *,
    approval_status: str | None = None,
    automation_error: str = "",
) -> dict[str, Any]:
    qualification = job.qualification
    reason = (
        qualification.reasons[0]
        if qualification and qualification.reasons
        else (qualification.hard_reject_reason if qualification else "ainda não classificada")
    )
    if automation_error:
        reason = f"Erro: {automation_error}"
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location_normalized or job.location,
        "score": job.score,
        "decision": qualification.decision if qualification else None,
        "hard_reject_reason": qualification.hard_reject_reason if qualification else None,
        "reason": reason,
        "missing_core": missing_core_technologies(job),
        "status": job.status.value,
        "approval_status": approval_status,
        "error": automation_error or None,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "bucket": _bucket(
            job,
            min_score,
            approval_status=approval_status,
            automation_error=automation_error,
        ),
    }


def compute_checks(job: JobOpportunity) -> dict[str, dict[str, Any]]:
    """Revalidações determinísticas exibidas antes do check do operador."""
    settings = _settings()
    stale = stale_reason(job, settings)
    text = " ".join(
        filter(
            None,
            [
                job.salary_raw,
                job.salary_text,
                job.analysis.summary if job.analysis else "",
            ],
        )
    )
    codes = detect_currencies(text) | explicit_currency_codes(
        job.currency, job.currency_signal
    )
    currency_reason = currency_rejection_reason(codes, ALLOWED_CURRENCIES)
    remote_ok = is_remote_location(job.location_normalized or job.location, job.title)
    score_ok = (job.score or 0) >= settings.qualification.min_score
    return {
        "remote": {
            "ok": remote_ok,
            "detail": "100% remoto" if remote_ok else "híbrido/presencial ou remoto não confirmado",
        },
        "currency": {
            "ok": currency_reason is None,
            "detail": currency_reason or ("USD/EUR ou sem moeda explícita" if not codes else "moeda elegível"),
        },
        "recency": {
            "ok": stale is None,
            "detail": stale
            or f"dentro da janela de {settings.search.expired_after_hours} horas",
        },
        "score": {
            "ok": score_ok,
            "detail": f"score {job.score if job.score is not None else '—'} / mínimo {settings.qualification.min_score}",
        },
    }


def job_detail(job_id: str) -> dict[str, Any] | None:
    repo = _repo()
    settings = _settings()
    job = repo.get_job(job_id)
    if job is None:
        return None
    approval = repo.get_approval_for_job(job.id)
    decision = repo.get_review_decision_for_job(job.id)
    erros = repo.failed_agent_events(AUTOMATION_CONSUMERS, job_id=job.id)
    automation_error = erros[0]["error"] or erros[0]["event_type"] if erros else ""
    application_getter = getattr(repo, "get_application_for_job", None)
    application = application_getter(job.id) if application_getter else None
    evidencias: list[dict[str, Any]] = [
        _serialize(item) for item in (application.evidence if application else [])
    ]
    vistos = {str(item.get("path", "")) for item in evidencias}
    checagem = (decision or {}).get("checks") or {}
    for item in checagem.get("evidence") or []:
        if str(item.get("path", "")) not in vistos:
            evidencias.append(_serialize(item))
            vistos.add(str(item.get("path", "")))
    evidencias = _fresh_evidence(job.id, evidencias, settings)
    return {
        "job": _serialize(job),
        "bucket": _bucket(
            job,
            settings.qualification.min_score,
            approval_status=approval.status if approval else None,
            automation_error=automation_error,
        ),
        "checks": compute_checks(job),
        "missing_core": missing_core_technologies(job),
        "approval": _serialize(approval) if approval else None,
        "review_decision": decision,
        "evidence": evidencias,
        "automation_error": automation_error or None,
        "can_check": bool(approval and approval.status == "PENDING"),
    }


def job_index(
    bucket: str = DEFAULT_BUCKET,
    *,
    query: str = "",
    sort: str = "newer",
    page: int = 1,
    limit: int = PAGE_SIZE,
) -> dict[str, Any]:
    """Lista paginada: filtro de texto, bucket, ordem e LIMIT resolvidos no SQL."""
    settings = _settings()
    repo = _repo()
    result = repo.query_review_jobs(
        bucket=bucket,
        query=query,
        sort=sort,
        page=page,
        limit=limit,
        automation_consumers=AUTOMATION_CONSUMERS,
    )
    approvals = {item.opportunity_id: item.status for item in repo.list_approvals()}
    errors: dict[str, str] = {}
    for event in repo.failed_agent_events(AUTOMATION_CONSUMERS):
        errors.setdefault(
            event["job_id"], event["error"] or event["event_type"] or "falha na automação"
        )
    items = [
        _summary_job(
            job,
            settings.qualification.min_score,
            approval_status=approvals.get(job.id),
            automation_error=errors.get(job.id, ""),
        )
        for job in result["items"]
    ]
    return {
        "counts": result["counts"],
        "items": items,
        "bucket": result["bucket"],
        "labels": dict(BUCKETS),
        "order": list(BUCKET_KEYS),
        "query": result["query"],
        "sort": result["sort"],
        "page": result["page"],
        "limit": result["limit"],
        "total": result["total"],
        "pages": result["pages"],
        "has_prev": result["page"] > 1,
        "has_next": result["page"] < result["pages"],
    }


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "ReviewPWA/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Evita log de cada request do service worker no console.
        return

    # ---- helpers ----
    def _authorized(self) -> bool:
        token = os.environ.get("REVIEW_TOKEN", "").strip()
        if not token:
            return True
        return self.headers.get("X-Review-Token", "") == token

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(_serialize(payload), ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _static(self, name: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "arquivo não encontrado")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or "javascript" in content_type:
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    # ---- rotas ----
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "token inválido")
            return
        if path == "/api/health":
            self._json({"ok": True})
            return
        if path == "/api/summary":
            try:
                self._json(job_index("all", limit=1)["counts"])
            except Exception as exc:  # pragma: no cover
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if path == "/api/jobs":
            params = parse_qs(urlparse(self.path).query, keep_blank_values=True)

            def first(key: str, default: str) -> str:
                return (params.get(key) or [default])[0]

            try:
                page = max(1, int(first("page", "1")))
            except ValueError:
                page = 1
            try:
                self._json(
                    job_index(
                        first("bucket", DEFAULT_BUCKET),
                        query=first("q", ""),
                        sort=first("sort", "newer"),
                        page=page,
                    )
                )
            except Exception as exc:  # pragma: no cover
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if path.startswith("/api/evidence/"):
            partes = path.removeprefix("/api/evidence/").split("/")
            if len(partes) != 2:
                self._error(HTTPStatus.BAD_REQUEST, "evidência inválida")
                return
            job_id, nome = partes
            alvo = resolve_evidence_path(job_id, nome)
            if alvo is None:
                self._error(HTTPStatus.NOT_FOUND, "evidência não encontrada")
                return
            settings = _settings()
            dias = settings.application.evidence_retention_days
            if file_is_expired(
                alvo, cutoff=retention_cutoff(dias), retention_days=dias
            ):
                cleanup_expired(
                    settings.resolve("data/evidence"),
                    retention_days=dias,
                    job_id=job_id,
                )
                self._error(HTTPStatus.NOT_FOUND, "evidência expirada (retenção 30 dias)")
                return
            tipo = mimetypes.guess_type(alvo.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, alvo.read_bytes(), tipo)
            return
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            try:
                detail = job_detail(job_id)
                if detail is None:
                    self._error(HTTPStatus.NOT_FOUND, "vaga não encontrada")
                else:
                    self._json(detail)
            except Exception as exc:  # pragma: no cover
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        static_names = {
            "/": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
            "/manifest.webmanifest": "manifest.webmanifest",
            "/sw.js": "sw.js",
            "/icon.svg": "icon.svg",
        }
        if path in static_names:
            self._static(static_names[path])
            return
        self._error(HTTPStatus.NOT_FOUND, "rota não encontrada")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "token inválido")
            return
        if not (path.startswith("/api/jobs/") and path.endswith(("/approve", "/reject"))):
            self._error(HTTPStatus.NOT_FOUND, "rota não encontrada")
            return
        action = "APPROVED" if path.endswith("/approve") else "REJECTED"
        job_id = path.removeprefix("/api/jobs/").rsplit("/", 1)[0]
        repo = _repo()
        job = repo.get_job(job_id)
        if job is None:
            self._error(HTTPStatus.NOT_FOUND, "vaga não encontrada")
            return
        approval = repo.get_approval_for_job(job.id)
        if approval is None or approval.status != "PENDING":
            self._error(
                HTTPStatus.CONFLICT,
                "esta vaga ainda não tem formulário preparado pelo agente; aguarde o application-serve",
            )
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        checks = compute_checks(job)
        if action == "APPROVED" and not all(item["ok"] for item in checks.values()):
            self._json(
                {
                    "error": "rechecks determinísticos reprovaram esta vaga",
                    "checks": checks,
                },
                HTTPStatus.CONFLICT,
            )
            return
        repo.resolve_approval(approval.id, action == "APPROVED")
        if action == "REJECTED":
            job.status = JobStatus.REJECTED_BY_FILTER
            repo.update_job(job)
        decision = repo.record_review_decision(
            job.id,
            action,
            approval_id=approval.id,
            reviewer="operator",
            checks=checks,
            note=str(payload.get("note", ""))[:500],
        )
        if action == "APPROVED":
            # Handoff persists across processes: the local submit worker claims
            # this event and re-runs every deterministic check before sending.
            repo.publish_agent_event(
                "apply_submit",
                "review.approved",
                job.id,
                {"approval_id": approval.id, "review_decision_id": decision["id"]},
            )
        self._json({"ok": True, "decision": decision})


def daily_evidence_cleanup(
    settings: Settings,
    *,
    interval_seconds: float = 86400.0,
    sleep: Any = time.sleep,
    stop: Any | None = None,
) -> None:
    """Varredura diária: remove evidências vencidas mesmos sem tráfego na PWA."""
    while True:
        cleanup_expired(
            settings.resolve("data/evidence"),
            retention_days=settings.application.evidence_retention_days,
        )
        if stop is not None and getattr(stop, "is_set", lambda: False)():
            return
        sleep(interval_seconds)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), ReviewHandler)
    server.daemon_threads = True
    settings = _settings()
    threading.Thread(
        target=daily_evidence_cleanup,
        args=(settings,),
        name="evidence-retention-daily",
        daemon=True,
    ).start()
    print(f"REVIEW_PWA=http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
