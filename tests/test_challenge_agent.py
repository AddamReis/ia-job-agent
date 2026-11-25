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

    def __init__(self, observations: list[dict], frames: list[FakeFrame] | None = None) -> None:
        self.observations = list(observations)
        self.locators: dict[str, FakeLocator] = {}
        self.frames = frames or []
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


class FakeElementHandle:
    """Iframe com posição antes/depois de ser trazido para o viewport."""

    def __init__(self, antes: dict, depois: dict) -> None:
        self.antes = antes
        self.depois = depois
        self.pinned = False
        self.scrolled = False

    def bounding_box(self) -> dict:
        return self.depois if self.pinned else self.antes

    def scroll_into_view_if_needed(self) -> None:
        self.scrolled = True

    def evaluate(self, _js: str) -> bool:
        self.pinned = True
        return True

    def get_attribute(self, _name: str) -> str:
        return ""


class FakeFrame:
    def __init__(
        self,
        url: str,
        elemento: FakeElementHandle,
        parent_frame: "FakeFrame | None" = None,
    ) -> None:
        self.url = url
        self.name = ""
        self._elemento = elemento
        self.parent_frame = parent_frame
        self.locators: dict[str, FakeLocator] = {}

    def frame_element(self) -> FakeElementHandle:
        return self._elemento

    def locator(self, seletor: str) -> FakeLocator:
        return self.locators.setdefault(seletor, FakeLocator())


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


def test_captcha_fora_do_viewport_e_trazido_com_overlay(tmp_path) -> None:
    # Reproduz o iCIMS da Avalara: iframe a ~-9864px, fora de qualquer print.
    elemento = FakeElementHandle(
        {"x": 120, "y": -9864, "width": 480, "height": 620},
        {"x": 400, "y": 50, "width": 480, "height": 620},
    )
    frame = FakeFrame("https://hcaptcha.com/1/challenge", elemento)
    pagina = FakePage([_obs(captcha=True), _obs(captcha=True)] + [_obs()] * 6, [frame])
    pagina.frames.insert(0, pagina)  # Playwright inclui o frame principal em page.frames
    llm = FakeLLM(CaptchaSolution(kind="text", answer="AB12", confidence=0.9))
    agente = ChallengeAgent(llm, FakeCredentials())

    assert agente.solve(pagina, evidence_dir=tmp_path) is True
    assert elemento.scrolled is True
    assert elemento.pinned is True
    assert pagina.locators['input[aria-label*="captcha" i]'].filled == ["AB12"]
    assert (tmp_path / "challenge_01.jpg").exists()


def test_captcha_ja_visivel_nao_e_reposicionado() -> None:
    caixa = {"x": 400, "y": 50, "width": 480, "height": 620}
    elemento = FakeElementHandle(caixa, caixa)
    frame = FakeFrame("https://hcaptcha.com/1/challenge", elemento)
    pagina = FakePage([_obs(captcha=True), _obs(captcha=True)] + [_obs()] * 6, [frame])
    pagina.frames.insert(0, pagina)
    agente = ChallengeAgent(
        FakeLLM(CaptchaSolution(kind="text", answer="OK", confidence=0.9)),
        FakeCredentials(),
    )

    assert agente.solve(pagina) is True
    assert elemento.pinned is False


def test_captcha_em_iframe_aninhado_fixa_pai_e_filho(tmp_path) -> None:
    fora = {"x": 10, "y": -9864, "width": 480, "height": 620}
    dentro = {"x": 0, "y": -1400, "width": 480, "height": 600}
    visivel = {"x": 400, "y": 50, "width": 480, "height": 620}
    externo = FakeFrame(
        "https://portal.example.com/frame", FakeElementHandle(fora, visivel)
    )
    interno = FakeFrame(
        "https://hcaptcha.com/1/challenge",
        FakeElementHandle(dentro, visivel),
        parent_frame=externo,
    )
    pagina = FakePage([_obs(captcha=True), _obs(captcha=True)] + [_obs()] * 6, [interno])
    pagina.frames.insert(0, pagina)
    agente = ChallengeAgent(
        FakeLLM(CaptchaSolution(kind="text", answer="OK", confidence=0.9)),
        FakeCredentials(),
    )

    assert agente.solve(pagina, evidence_dir=tmp_path) is True
    assert externo._elemento.pinned is True
    assert interno._elemento.pinned is True


def test_captcha_de_texto_dentro_de_frame_e_preenchido() -> None:
    frame = FakeFrame(
        "https://hcaptcha.com/1/challenge",
        FakeElementHandle({"x": 0, "y": 0, "width": 300, "height": 80}, {}),
    )
    seletor = 'input[aria-label*="captcha" i]'
    frame.locators[seletor] = FakeLocator()
    pagina = FakePage([], [frame])

    assert ChallengeAgent._fill_captcha_text(pagina, "DENTRO") is True
    assert frame.locators[seletor].filled == ["DENTRO"]
