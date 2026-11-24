"""PWA de revisão das classificações e do check de autorização do Apply.

Servidor HTTP da própria stdlib: nenhuma dependência nova é necessária na DGX.
Os dados vêm do mesmo Postgres do agente. O Tailscale Serve publica este
servidor em HTTPS dentro da tailnet, o que permite instalá-lo como PWA.
"""
from __future__ import annotations

import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_CURRENCIES = {"USD", "EUR"}


def _settings() -> Settings:
    return Settings.load()


def _repo() -> Repository:
    return Repository()


def _serialize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(v) for v in value]
    return value


def _bucket(job: JobOpportunity, min_score: int) -> str:
    qualification = job.qualification
    if job.status in {JobStatus.SUBMITTED, JobStatus.APPLYING, JobStatus.PENDING_APPROVAL}:
        return "approved"
    if qualification and qualification.decision == "APPLY" and (job.score or 0) >= min_score:
        return "approved"
    if (
        (qualification and qualification.decision == "SKIP")
        or job.status
        in {
            JobStatus.REJECTED_BY_FILTER,
            JobStatus.EXPIRED,
            JobStatus.FAILED,
            JobStatus.REQUIRES_HUMAN_ACTION,
        }
    ):
        return "rejected"
    return "pending"


def _summary_job(job: JobOpportunity, min_score: int) -> dict[str, Any]:
    qualification = job.qualification
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location_normalized or job.location,
        "score": job.score,
        "decision": qualification.decision if qualification else None,
        "hard_reject_reason": qualification.hard_reject_reason if qualification else None,
        "reason": (
            qualification.reasons[0]
            if qualification and qualification.reasons
            else (qualification.hard_reject_reason if qualification else "ainda não classificada")
        ),
        "status": job.status.value,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "bucket": _bucket(job, min_score),
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
            "detail": stale or "dentro da janela de 24 horas",
        },
        "score": {
            "ok": score_ok,
            "detail": f"score {job.score if job.score is not None else '—'} / mínimo {settings.qualification.min_score}",
        },
    }


def job_detail(job_id: str) -> dict[str, Any] | None:
    repo = _repo()
    job = repo.get_job(job_id)
    if job is None:
        return None
    approval = repo.get_approval_for_job(job.id)
    decision = repo.get_review_decision_for_job(job.id)
    return {
        "job": _serialize(job),
        "bucket": _bucket(job, _settings().qualification.min_score),
        "checks": compute_checks(job),
        "approval": _serialize(approval) if approval else None,
        "review_decision": decision,
        "can_check": bool(approval and approval.status == "PENDING"),
    }


def job_index(bucket: str = "all") -> dict[str, Any]:
    settings = _settings()
    min_score = settings.qualification.min_score
    jobs = _repo().list_jobs(limit=5000)
    items = [_summary_job(job, min_score) for job in jobs]
    counts = {
        "pending": sum(i["bucket"] == "pending" for i in items),
        "rejected": sum(i["bucket"] == "rejected" for i in items),
        "approved": sum(i["bucket"] == "approved" for i in items),
        "all": len(items),
    }
    if bucket != "all":
        items = [i for i in items if i["bucket"] == bucket]
    return {"counts": counts, "items": items, "bucket": bucket}


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
                self._json(job_index("all")["counts"])
            except Exception as exc:  # pragma: no cover
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        if path == "/api/jobs":
            bucket = urlparse(self.path).query.split("bucket=", 1)
            bucket_value = bucket[1].split("&", 1)[0] if len(bucket) > 1 else "all"
            try:
                self._json(job_index(bucket_value))
            except Exception as exc:  # pragma: no cover
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
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


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), ReviewHandler)
    server.daemon_threads = True
    print(f"REVIEW_PWA=http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
