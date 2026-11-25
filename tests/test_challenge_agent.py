"""Skill de desafios: reconhecimento, visão e uso somente de segredo explícito."""
from __future__ import annotations

import json

from src.execution.web.challenge_agent import CaptchaSolution, ChallengeAgent


class FakeLocator:
    def __init__(self, total: int = 1) -> None:
        self.total = total
        self.filled: list[str] = []

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return self.total

    def fill(self, value: str) -> None:
        self.filled.append(value)

    def input_value(self) -> str:
        return ""

    def click(self) -> None:
        return None

    def nth(self, _: int) -> "FakeLocator":
        return self


class FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))


class FakePage:
    url = "https://portal.example.com/apply"

    def __init__(self, observations: list[dict]) -> None:
        self.observations = list(observations)
        self.locators: dict[str, FakeLocator] = {}
        self.mouse = FakeMouse()

    def evaluate(self, _: str) -> dict:
        return self.observations.pop(0) if self.observations else {}

    def screenshot(self, **_: object) -> bytes:
        return b"jpeg-falso"

    def locator(self, seletor: str) -> FakeLocator:
        return self.locators.setdefault(seletor, FakeLocator())

    def get_by_role(self, *_: object, **__: object) -> FakeLocator:
        return FakeLocator(total=0)

    def wait_for_timeout(self, _: int) -> None:
        return None


class FakeCredentials:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = secrets or {}

    def get_secret(self, _domain: str, kind: str) -> str:
        return self.secrets.get(kind, "")


class FakeLLM:
    def __init__(self, solution: CaptchaSolution) -> None:
        self.solution = solution
        self.mensagens: list[dict] = []

    def structured(self, messages: list[dict], _schema: type, **_: object) -> CaptchaSolution:
        self.mensagens = messages
        return self.solution


def _obs(*, captcha: bool = False, passwords: int = 0, mfa: bool = False) -> dict:
    return {
        "text": "Formulário de candidatura",
        "captcha": captcha,
        "passwords": passwords,
        "mfa": mfa,
        "inputs": (
            [{"type": "text", "aria": "captcha"}] if captcha
            else [{"type": "password", "aria": "password"} for _ in range(passwords)]
        ),
        "captcha_frames": [],
        "viewport": {"w": 1000, "h": 800},
    }


def test_captcha_text_e_resolvido_pela_visao(tmp_path) -> None:
    pagina = FakePage([_obs(captcha=True), _obs(captcha=True)] + [_obs()] * 4)
    llm = FakeLLM(CaptchaSolution(kind="text", answer="AB12", confidence=0.9))
    agente = ChallengeAgent(llm, FakeCredentials())

    resolvido = agente.solve(pagina, evidence_dir=tmp_path)

    assert resolvido is True
    conteudo = json.dumps(llm.mensagens, ensure_ascii=False)
    assert "data:image/jpeg;base64" in conteudo
    assert pagina.locators['input[aria-label*="captcha" i]'].filled == ["AB12"]
    assert (tmp_path / "challenge_01.jpg").exists()


def test_captcha_de_imagem_clica_nos_pontos_devolvidos() -> None:
    pagina = FakePage([_obs(captcha=True), _obs(captcha=True)] + [_obs()] * 4)
    llm = FakeLLM(
        CaptchaSolution(kind="image", points=[[0.25, 0.5], [0.75, 0.5]], confidence=0.8)
    )
    agente = ChallengeAgent(llm, FakeCredentials())

    assert agente.solve(pagina) is True
    assert pagina.mouse.clicks == [(250.0, 400.0), (750.0, 400.0)]


def test_sem_segredo_no_cofre_o_agente_nao_inventa_credencial(tmp_path) -> None:
    pagina = FakePage([_obs(passwords=1)] * 6)
    agente = ChallengeAgent(FakeLLM(CaptchaSolution()), FakeCredentials())

    assert agente.solve(pagina, evidence_dir=tmp_path) is False
    assert not pagina.locators.get('input[type="password"]')


def test_senha_do_cofre_e_usada_quando_existe() -> None:
    pagina = FakePage([_obs(passwords=1), _obs(passwords=1)] + [_obs()] * 4)
    pagina.locators['input[type="password"]'] = FakeLocator()
    agente = ChallengeAgent(
        FakeLLM(CaptchaSolution()), FakeCredentials({"password": "segredo-do-cofre"})
    )

    assert agente.solve(pagina) is True
    assert pagina.locators['input[type="password"]'].filled == ["segredo-do-cofre"]
