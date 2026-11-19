"""Camada compartilhada (hexagonal): configuração, domínio, portas e relógio.

Os módulos aqui não dependem de nenhum agente. É a fronteira estável usada
por discovery, classification, apply e fast lane.
"""
from . import config, discovery_window, domain, ports, seed

__all__ = ["config", "discovery_window", "domain", "ports", "seed"]
