"""Janela determinística do discovery-agent.

O ponto central é a âncora: ``reference_time`` é lida uma única vez, antes de
qualquer query. Mesmo que a execução dure uma hora, todas as localidades usam
a mesma referência — não ``datetime.now()`` dentro do loop.

O cutoff final é o **mais antigo** entre:

* ``reference_time - (janela_do_slot + sobreposição)``; e
* ``ultimo_inicio_bem_sucedido - sobreposição`` (watermark).

Isso cobre tanto a primeira execução do dia (que encontra vagas da noite)
quanto falhas/intervalos entre execuções.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DiscoveryScheduleConfig, Settings

_FALLBACK_TZ = timezone(timedelta(hours=-3))  # Brasil sem DST desde 2019
_FILTER_HOURS = {"day": 24, "week": 168}


def local_timezone(name: str) -> tzinfo:
    """ZoneInfo quando disponível; fallback fixo -03:00 no Windows sem tzdata."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return _FALLBACK_TZ


def local_now(name: str) -> datetime:
    return datetime.now(local_timezone(name))


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def normalize_slot(cfg: DiscoveryScheduleConfig, slot: str = "auto", now: datetime | None = None) -> str:
    """Resolve ``first``/``regular``/``auto`` para um horário literal configurado."""
    first = cfg.first_slot
    literal = set(cfg.slots) | {first}
    key = (slot or "auto").strip().lower()
    if key in {"first", "primeira", "primeiro", "abertura"}:
        return first
    if key in {"regular", "normal", "demais"}:
        return next((item for item in cfg.slots if item != first), first)
    if key in literal:
        return key
    if key not in {"auto", ""}:
        raise ValueError(f"Slot desconhecido: {slot!r}")
    current = (now or local_now(cfg.timezone)).time()
    slots = cfg.slots or [first]
    ultimo = _parse_hhmm(slots[-1])
    if _minutes(current) > _minutes(ultimo) + 60:
        return first  # fora do range: próxima abertura do dia
    return min(slots, key=lambda item: abs(_minutes(_parse_hhmm(item)) - _minutes(current)))


@dataclass(frozen=True)
class DiscoveryWindow:
    slot: str
    reference_time: datetime
    cutoff: datetime
    hours: float
    buffer_minutes: int
    time_filter: str
    source: str  # "window" | "watermark"
    watermark_started_at: datetime | None = None

    @property
    def span_hours(self) -> float:
        return (self.reference_time - self.cutoff).total_seconds() / 3600

    def describe(self) -> str:
        return (
            f"slot={self.slot} reference={self.reference_time.isoformat(timespec='seconds')} "
            f"cutoff={self.cutoff.isoformat(timespec='seconds')} "
            f"span={self.span_hours:.2f}h source={self.source} filter={self.time_filter}"
        )


def _aware(value: datetime, tz: tzinfo) -> datetime:
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


def compute_window(
    settings: Settings,
    slot: str = "auto",
    *,
    now: datetime | None = None,
    last_started: datetime | None = None,
) -> DiscoveryWindow:
    """Calcula a janela de uma execução, sem consultar o relógio mais de uma vez."""
    cfg = settings.discovery_schedule
    resolved_slot = normalize_slot(cfg, slot, now)
    tz = local_timezone(cfg.timezone)
    reference = _aware(now or datetime.now(tz), tz)
    hours = float(cfg.first_run_hours if resolved_slot == cfg.first_slot else cfg.regular_hours)
    buffer = timedelta(minutes=cfg.overlap_buffer_minutes)

    by_window = reference - timedelta(hours=hours) - buffer
    by_watermark = None
    if cfg.use_last_watermark and last_started is not None:
        by_watermark = _aware(last_started, tz) - buffer

    source = "window"
    if by_watermark is not None and by_watermark < by_window:
        cutoff = by_watermark
        source = "watermark"
    else:
        cutoff = by_window

    time_filter = cfg.linkedin_ui_filter
    max_hours = _FILTER_HOURS.get(time_filter, 24)
    if cfg.auto_widen_time_filter and (reference - cutoff).total_seconds() > max_hours * 3600:
        time_filter = "week"
        max_hours = _FILTER_HOURS["week"]
        cutoff = max(cutoff, reference - timedelta(hours=max_hours))

    return DiscoveryWindow(
        slot=resolved_slot,
        reference_time=reference,
        cutoff=cutoff,
        hours=hours,
        buffer_minutes=cfg.overlap_buffer_minutes,
        time_filter=time_filter,
        source=source,
        watermark_started_at=_aware(last_started, tz) if last_started is not None else None,
    )


__all__ = ["DiscoveryWindow", "compute_window", "local_now", "local_timezone", "normalize_slot"]
