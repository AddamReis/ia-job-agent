"""Evidências do Apply em disco local, com retenção configurável.

Os arquivos ficam em ``data/evidence/<job_id>/``. O Postgres guarda só caminho
relativo e hash — sem ``bytea`` e sem gzip sobre WebP/JPEG (o ganho seria
marginal e a leitura na PWA pagaria CPU extra). A limpeza considera a data de
criação gravada no metadados e, na falta dela, o mtime do arquivo.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from ..core.logging import log_event

# PNG continua legível apenas para evidências antigas; capturas novas são WebP
# (com fallback para JPEG quando o CDP não oferece codificação WebP).
EVIDENCE_SUFFIXES = frozenset({".webp", ".jpg", ".jpeg", ".png"})
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def evidence_root(settings: Any) -> Path:
    """Raiz das evidências resolvida a partir da configuração ativa."""
    return settings.resolve("data/evidence")


def retention_cutoff(retention_days: int, now: datetime | None = None) -> datetime:
    """Limite inferior da janela; ``retention_days <= 0`` desativa a retenção."""
    referencia = now or datetime.now()
    return referencia - timedelta(days=max(0, retention_days))


def _created_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    texto = str(value or "").strip()
    if not texto:
        return None
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(texto)
    except ValueError:
        return None
    # created_at das evidências é gravado com datetime.now() (naive/local).
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def is_expired(item: Any, *, cutoff: datetime, retention_days: int) -> bool:
    """True quando o metadado saiu da janela de consulta."""
    if retention_days <= 0:
        return False
    valor = (
        item.get("created_at")
        if isinstance(item, Mapping)
        else getattr(item, "created_at", None)
    )
    criado = _created_at(valor)
    # Metadado legado sem data não pode ser provado como vencido: a varredura
    # por mtime rémove o arquivo, e a PWA esconde o link quando ele some.
    return criado is not None and criado < cutoff


def file_is_expired(path: Path, *, cutoff: datetime, retention_days: int) -> bool:
    """True quando o arquivo saiu da janela (usa o mtime como fonte)."""
    if retention_days <= 0:
        return False
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) < cutoff
    except OSError:
        return True


def cleanup_expired(
    root: str | Path,
    *,
    retention_days: int,
    now: datetime | None = None,
    job_id: str | None = None,
) -> dict[str, int]:
    """Remove evidências vencidas e diretórios que ficam vazios.

    ``job_id`` restringe a limpeza a uma vaga (usado no envio e na consulta);
    sem ele a varredura é global (job diário). Nunca followa symlinks para fora
    da raiz: só arquivos regulares com sufixo de imagem são candidatos.
    """
    counters = {"files_removed": 0, "bytes_removed": 0, "dirs_removed": 0}
    if retention_days <= 0:
        return counters
    base = Path(root)
    if not base.is_dir():
        return counters
    cutoff = retention_cutoff(retention_days, now)
    if job_id:
        if not _JOB_ID_RE.fullmatch(job_id):
            return counters
        albums = [base / job_id]
    else:
        albums = [
            item
            for item in base.iterdir()
            if item.is_dir() and not item.is_symlink() and _JOB_ID_RE.fullmatch(item.name)
        ]
    for album in albums:
        if not album.is_dir() or album.is_symlink():
            continue
        for arquivo in album.iterdir():
            if (
                arquivo.is_symlink()
                or not arquivo.is_file()
                or arquivo.suffix.lower() not in EVIDENCE_SUFFIXES
            ):
                continue
            if not file_is_expired(arquivo, cutoff=cutoff, retention_days=retention_days):
                continue
            try:
                tamanho = arquivo.stat().st_size
                arquivo.unlink()
            except OSError:
                continue
            counters["files_removed"] += 1
            counters["bytes_removed"] += tamanho
        try:
            if not any(album.iterdir()):
                album.rmdir()
                counters["dirs_removed"] += 1
        except OSError:
            pass
    if counters["files_removed"] or counters["dirs_removed"]:
        log_event("evidence_retention_cleanup", job_id=job_id or "all", **counters)
    return counters
