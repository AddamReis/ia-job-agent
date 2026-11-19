"""Cliente Gmail oficial com OAuth e escopo mínimo (somente leitura)."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from email.message import EmailMessage

from ...core.config import Settings
from ...core.errors import AgentError

try:  # importado apenas quando o módulo for usado
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover
    Request = None  # type: ignore
    Credentials = None  # type: ignore
    InstalledAppFlow = None  # type: ignore
    build = None  # type: ignore


@dataclass
class GmailMessage:
    id: str
    subject: str
    sender: str
    date: str
    body: str


class GmailClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service = None

    def _token(self) -> str | None:
        try:
            import keyring
        except ImportError:  # pragma: no cover
            return None
        return keyring.get_password(
            self.settings.gmail.keyring_service, self.settings.gmail.keyring_username
        )

    def _credentials(self):
        token = self._token()
        creds = None
        if token:
            try:
                creds = Credentials.from_authorized_user_info(
                    json.loads(token), self.settings.gmail.scopes
                )
            except Exception:
                creds = None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            secrets = self.settings.resolve(self.settings.gmail.credentials_file)
            if not secrets.exists():
                raise AgentError(
                    "Gmail OAuth não configurado. Coloque o client secrets em "
                    f"{secrets} e rode: agent gmail-auth"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(secrets), self.settings.gmail.scopes
            )
            creds = flow.run_local_server(port=0)
            import keyring

            keyring.set_password(
                self.settings.gmail.keyring_service,
                self.settings.gmail.keyring_username,
                creds.to_json(),
            )
        return creds

    def service(self):
        if self._service is None:
            if build is None:
                raise AgentError("Bibliotecas do Gmail não instaladas")
            self._service = build("gmail", "v1", credentials=self._credentials())
        return self._service

    def search(self, query: str, max_results: int | None = None) -> list[GmailMessage]:
        limit = max_results or self.settings.gmail.max_results
        service = self.service()
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=limit)
            .execute()
        )
        messages: list[GmailMessage] = []
        for item in response.get("messages", []):
            raw = (
                service.users()
                .messages()
                .get(userId="me", id=item["id"], format="full")
                .execute()
            )
            headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
            body = self._body(raw.get("payload", {}))
            messages.append(
                GmailMessage(
                    id=item["id"],
                    subject=headers.get("subject", ""),
                    sender=headers.get("from", ""),
                    date=headers.get("date", ""),
                    body=body[:8000],
                )
            )
        return messages

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        html: bool = False,
        trace_id: str = "",
    ) -> str:
        """Envia texto/HTML com trace ID no cabeçalho. Retorna o ID da mensagem."""
        message = EmailMessage()
        message["To"] = to or "me"
        message["From"] = "me"
        message["Subject"] = subject
        if trace_id:
            # Permite localizar no Gmail pelas mesmas IDs usadas em logs/banco.
            message["X-Job-Agent-Trace-Id"] = trace_id
        if html:
            message.set_content(re.sub(r"<[^>]+>", " ", body))
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        sent = (
            self.service()
            .users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return str(sent.get("id", ""))

    @staticmethod
    def _body(payload: dict) -> str:
        if "data" in payload and payload.get("mimeType") == "text/plain":
            return GmailClient._decode(payload["data"])
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain" and "data" in part:
                return GmailClient._decode(part["data"])
            nested = GmailClient._body(part)
            if nested:
                return nested
        return payload.get("snippet", "")

    @staticmethod
    def _decode(data: str) -> str:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
