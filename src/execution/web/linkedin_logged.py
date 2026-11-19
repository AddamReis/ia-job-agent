"""Sondagem de Easy Apply no navegador JÁ LOGADO do usuário (via CDP).

Abre uma aba nova no perfil logado, verifica a presença do botão de candidatura
simplificada no DOM e fecha a aba. Sem login automatizado, sem API privada, sem
evasão: é exatamente a página que o usuário vê, lida via Chrome DevTools Protocol.

A sondagem ``has_easy_apply`` e a leitura de perfil/empresas são SEMPRE sem clique.
O envio agêntico via browser é feito à parte, por ``BrowserApplyAgent``, e exige
aprovação humana persistida antes de qualquer clique em Submit.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from ...core.logging import log_event

# Rótulos reais vistos na interface logada do LinkedIn (EN/PT).
_EASY_LABELS = (
    "Easy Apply to this job",
    "Easy Apply",
    "Candidatura simplificada",
    "Candidatura rápida",
    "Candidatura rapida",
)
_EXTERNAL_LABELS = (
    "Apply on company website",
    "Apply externally",
    "Solicitar no site da empresa",
)


class LoggedBrowserUnavailable(RuntimeError):
    """O Chrome logado não está acessível via CDP."""


class LinkedInLoggedBrowser:
    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", timeout_ms: int = 60000) -> None:
        self.cdp_url = cdp_url
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None

    def connect(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise LoggedBrowserUnavailable("playwright não instalado") from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
            contexts = self._browser.contexts
            if not contexts:
                raise LoggedBrowserUnavailable("nenhum contexto logado disponível")
            self._context = contexts[0]
            log_event("logged_browser_connected", cdp_url=self.cdp_url)
        except Exception as exc:
            self.close()
            raise LoggedBrowserUnavailable(str(exc)) from exc

    def is_connected(self) -> bool:
        return self._context is not None

    def close(self) -> None:
        # NUNCA chamar browser.close() NEM playwright.stop(): em um browser
        # conectado via CDP ambos derrubam o endpoint 9222 do Chrome do usuario.
        # Apenas fecha as abas de vaga criadas por nos. O driver Playwright é
        # encerrado quando o processo Python termina.
        try:
            if self._context is not None:
                for page in list(self._context.pages):
                    try:
                        if page.url and "/jobs/view/" in page.url:
                            page.close()
                    except Exception:
                        pass
        except Exception:
            pass

    def open_tab(self, url: str, *, settle_seconds: float = 2.5):
        """Abre uma aba de vaga/portal externo e a devolve ao chamador.

        A aba permanece aberta enquanto a aprovação humana é solicitada; quem
        a abriu deve chamá-la novamente em ``close_tab``. Nunca fechamos o
        navegador nem o contexto, e nunca trazemos a janela para a frente.
        """
        self.connect()
        job_id = self._job_id(url)
        reused = False
        page = None
        # Reaproveita uma aba já carregada da MESMA vaga. Abrir muitas abas
        # seguidas no perfil debug dispara lentidão/verificações do LinkedIn.
        if job_id:
            for candidate in self._context.pages:  # type: ignore[union-attr]
                try:
                    candidate_url = candidate.url or ""
                    if f"/jobs/view/{job_id}" not in candidate_url:
                        continue
                    if any(
                        mark in candidate_url.lower()
                        for mark in ("recaptcha", "ns1p.net", "protechts.net", "challenge")
                    ):
                        continue
                    state = candidate.evaluate(
                        "({t:document.title,r:document.readyState})"
                    )
                    if state.get("t") and state.get("r") == "complete":
                        page = candidate
                        reused = True
                        break
                except Exception:
                    continue
        created = page is None
        if created:
            page = self._context.new_page()  # type: ignore[union-attr]
        try:
            if not reused:
                try:
                    response = page.goto(
                        url, wait_until="domcontentloaded", timeout=self.timeout_ms
                    )
                except Exception:
                    # O Chrome logado pode responder devagar; se a página já
                    # estiver de fato carregada, aproveitamos em vez de falhar.
                    page.wait_for_timeout(3000)
                    state = page.evaluate("({t:document.title,r:document.readyState})")
                    if not (state.get("t") and state.get("r") == "complete"):
                        raise
                    response = None
                if response is not None and response.status >= 400:
                    raise LoggedBrowserUnavailable(f"HTTP {response.status} ao abrir {url}")
            page.wait_for_timeout(int(settle_seconds * 1000))
            return page
        except Exception:
            if created:
                self.close_tab(page)
            raise

    def close_tab(self, page) -> None:
        try:
            if page is not None and not page.is_closed():
                page.close()
        except Exception:
            pass

    @staticmethod
    def _job_id(url: str) -> str:
        match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})", url or "", re.I)
        return match.group(1) if match else ""

    def _focused_page(self):
        """Retorna a aba atualmente em foco (ou a primeira como fallback)."""
        for page in self._context.pages:  # type: ignore[union-attr]
            try:
                if page.evaluate("document.hasFocus()"):
                    return page
            except Exception:
                continue
        pages = self._context.pages  # type: ignore[union-attr]
        return pages[0] if pages else None

    def has_easy_apply(self, url: str, *, settle_seconds: float = 2.5) -> bool | None:
        """Retorna True/False com base no DOM logado, ou None se não conseguir ler.

        Abre uma aba nova, espera o conteúdo, procura o botão exato e fecha a aba.
        A janela do Chrome debug fica OCULTA (SW_HIDE) o tempo todo — nunca
        trazemos a janela para a frente, para não atrapalhar o uso do computador.
        Nenhum clique de candidatura é feito aqui.
        """
        self.connect()
        page = self._context.new_page()  # type: ignore[union-attr]
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is not None and response.status >= 400:
                log_event("logged_probe_bad_status", status=response.status, url=url)
                return None
            page.wait_for_timeout(int(settle_seconds * 1000))

            # Seletores explícitos de botão — evita confundir texto de descrição.
            for label in _EASY_LABELS:
                selectors = (
                    f'button[aria-label="{label}"]',
                    f'button:has-text("{label}")',
                    f'[role="button"]:has-text("{label}")',
                )
                for selector in selectors:
                    try:
                        if page.locator(selector).count() > 0:
                            log_event(
                                "easy_apply_found_logged_browser",
                                external_id=self._job_id(url),
                                label=label,
                            )
                            return True
                    except Exception:
                        continue

            # Confirma ausência: se houver botão de site externo, é definitivamente False.
            body = (page.content() or "").lower()
            if any(label.lower() in body for label in _EXTERNAL_LABELS):
                log_event("easy_apply_external_found", external_id=self._job_id(url))
                return False
            return False
        except Exception as exc:
            log_event(
                "logged_probe_failed",
                external_id=self._job_id(url),
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass
            # Nao chamar bring_to_front(): a janela debug deve permanecer oculta.


    # ---- leitura auxiliar do perfil logado (somente leitura) ----

    def read_profile_skills(self, profile_url: str) -> list[str]:
        """Lê as skills reais da página de perfil logada (sem alterar nada).

        O componente de cada skill tem um id estável
        ``com.linkedin.sdui.profile.skill(...)``. O primeiro ``<p>`` dentro dele
        é o NOME da competência; os demais textos são evidências/certificados.
        """
        self.connect()
        base = (profile_url or "").strip().rstrip("/")
        if not base:
            return []
        if "/details/skills" not in base:
            base = f"{base}/details/skills/"
        page = self._context.new_page()  # type: ignore[union-attr]
        try:
            response = page.goto(base, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is not None and response.status >= 400:
                log_event("linkedin_skills_bad_status", status=response.status)
                return []
            page.wait_for_timeout(2500)
            selector = '[id*="com.linkedin.sdui.profile.skill("]'
            page.wait_for_selector(selector, timeout=15000)
            nodes = page.locator(selector)
            skills: list[str] = []
            seen: set[str] = set()
            for index in range(nodes.count()):
                node = nodes.nth(index)
                node_id = node.get_attribute("id") or ""
                if node_id.endswith("-divider"):
                    continue
                try:
                    paragraphs = node.locator("p")
                    if paragraphs.count() == 0:
                        continue
                    name = paragraphs.first.inner_text(timeout=1000).strip()
                except Exception:
                    continue
                key = name.casefold()
                if name and len(name) <= 120 and key not in seen:
                    seen.add(key)
                    skills.append(name)
            log_event("linkedin_skills_read", count=len(skills))
            return skills
        except Exception as exc:
            log_event(
                "linkedin_skills_read_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return []
        finally:
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _company_slug(company: str) -> list[str]:
        """Gera slugs prováveis para a página da empresa no LinkedIn."""
        base = (company or "").casefold()
        base = re.sub(r"\b(llc|ltda|s/?a|tecnologia|technology|sa)\b", " ", base)
        base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        if not base:
            return []
        parts = [part for part in base.split("-") if part]
        candidates = [base]
        # "Compasso UOL" redireciona no LinkedIn para "compass-uol".
        if len(parts) > 1 and parts[0].endswith("o"):
            candidates.append("-".join([parts[0][:-1], *parts[1:]]))
        if len(parts) > 1:
            candidates.append(parts[0])
        # remove acentos comuns de nomes em português
        sem_acento = (
            base.replace("á", "a").replace("ã", "a").replace("ç", "c")
            .replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
        )
        if sem_acento != base:
            candidates.insert(0, sem_acento)
        vistos: set[str] = set()
        return [item for item in candidates if not (item in vistos or vistos.add(item))]

    @staticmethod
    def _company_page_url(company_url: str) -> str:
        """Normaliza um link /company/... para a aba de vagas da empresa."""
        match = re.search(r"/company/([^/?#]+)", company_url or "", re.I)
        if not match:
            return ""
        return f"https://www.linkedin.com/company/{match.group(1)}/jobs/"

    @staticmethod
    def _job_ids_from_company_html(html: str) -> list[str]:
        """Extrai IDs de vagas do HTML renderizado da página da empresa.

        A SPA nova não usa mais `a[href*="/jobs/view/"]` na listagem; os IDs
        aparecem em `originToLandingJobPostings` (URL-encoded) e em `currentJobId`.
        """
        ids: list[str] = []

        def add(value: str) -> None:
            for item in re.findall(r"\d{6,}", value):
                if item not in ids:
                    ids.append(item)

        for block in re.findall(r"originToLandingJobPostings=([^&\"']+)", html, re.I):
            add(unquote(block))
        for pattern in (r"currentJobId[=:][\"']?(\d{6,})", r"jobId[=:][\"']?(\d{6,})"):
            for value in re.findall(pattern, html, re.I):
                add(value)
        for value in re.findall(r"/jobs/view/(?:[^/?#\"']*-)?(\d{6,})", html, re.I):
            add(value)
        return ids

    def _discover_company_page(
        self,
        company: str,
        *,
        company_url: str = "",
        job_url: str = "",
    ) -> str:
        """Descobre a URL /company/<slug>/jobs/ por fontes em ordem de confiança."""
        page = self._context.new_page()  # type: ignore[union-attr]
        candidates: list[str] = []
        normalize = self._company_page_url(company_url)
        if normalize:
            candidates.append(normalize)
        if job_url and not candidates:
            detail = self.read_job_detail(job_url)
            if detail and detail.get("company_url"):
                candidates.append(self._company_page_url(detail["company_url"]))
        candidates.extend(
            f"https://www.linkedin.com/company/{slug}/jobs/"
            for slug in self._company_slug(company)
        )
        try:
            for url in candidates:
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    page.wait_for_timeout(1800)
                    final = page.url
                    if response is not None and response.status >= 400:
                        continue
                    if "/company/unavailable" in final or "/company/" not in final:
                        continue
                    if not page.locator("h1").count():
                        continue
                    log_event("company_page_found", company=company, url=final)
                    return final
                except Exception:
                    continue
            return ""
        finally:
            try:
                page.close()
            except Exception:
                pass

    def search_company_job_urls(
        self,
        company: str,
        *,
        limit: int = 3,
        company_url: str = "",
        job_url: str = "",
    ) -> list[str]:
        """Busca URLs de vagas recentes da empresa no LinkedIn logado.

        Usa a página `/company/<slug>/jobs/` — a mesma que o usuário vê — e lê
        os IDs renderizados no HTML. Sem API privada, sem clique em candidatura.
        """
        self.connect()
        base_url = self._discover_company_page(
            company, company_url=company_url, job_url=job_url
        )
        if not base_url:
            log_event("company_page_not_found", company=company)
            return []
        page = self._context.new_page()  # type: ignore[union-attr]
        try:
            response = page.goto(base_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is not None and response.status >= 400:
                log_event("company_jobs_bad_status", status=response.status, company=company)
                return []
            page.wait_for_timeout(2500)
            ids = self._job_ids_from_company_html(page.content() or "")
            found = [f"https://www.linkedin.com/jobs/view/{item}" for item in ids]
            log_event(
                "company_jobs_found",
                company=company,
                url=base_url,
                count=len(found),
            )
            return found[: max(limit * 4, 12)]
        except Exception as exc:
            log_event(
                "company_jobs_search_failed",
                company=company,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []
        finally:
            try:
                page.close()
            except Exception:
                pass

    def read_job_detail(self, url: str) -> dict[str, str] | None:
        """Lê título/empresa/local/descrição de uma vaga logada, sem clicar."""
        self.connect()
        page = self._context.new_page()  # type: ignore[union-attr]

        def first_text(selector: str) -> str:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    return ""
                return (locator.first.inner_text(timeout=1500) or "").strip()
            except Exception:
                return ""

        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is not None and response.status >= 400:
                return None
            page.wait_for_timeout(1800)
            description = first_text(
                ".show-more-less-html__markup, .jobs-description-content__text, "
                ".description__text"
            )
            if not description:
                body_text = first_text("body")
                for heading in ("Sobre a vaga", "About the job", "Job details"):
                    index = body_text.find(heading)
                    if index < 0:
                        continue
                    section = body_text[index + len(heading) :]
                    for stop in (
                        "Pessoas que você pode contatar",
                        "People you may know",
                        "People also viewed",
                    ):
                        end = section.find(stop)
                        if end > 0:
                            section = section[:end]
                    description = section.strip()
                    break
                if not description:
                    description = body_text
            if not description:
                return None

            # A SPA nova não tem h1/topcard; o título da aba traz
            # "Vaga | Empresa | LinkedIn".
            title = first_text(".top-card-layout__title, h1")
            company = first_text(".topcard__org-name-link, .topcard__flavor")
            page_title = (page.title() or "").strip()
            if page_title.endswith("| LinkedIn"):
                page_title = page_title[: -len("| LinkedIn")].strip()
            if not title or not company:
                parts = [part.strip() for part in page_title.split("|") if part.strip()]
                if len(parts) >= 2:
                    title = title or " | ".join(parts[:-1])
                    company = company or parts[-1]
                elif parts:
                    title = title or parts[0]

            company_url = ""
            company_anchors = page.locator('a[href*="/company/"]')
            for index in range(min(company_anchors.count(), 10)):
                href = company_anchors.nth(index).get_attribute("href") or ""
                if re.search(r"/company/[^/?#]+", href):
                    company_url = href
                    break
            return {
                "url": url,
                "title": title,
                "company": company,
                "company_url": company_url,
                "location": first_text(
                    ".topcard__flavor--bullet, .top-card-layout__second-subline"
                ),
                "description": description[:12000],
            }
        except Exception as exc:
            log_event(
                "company_job_detail_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

