"""Representação normalizada de um campo de formulário."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class FormField:
    label: str = ""
    field_type: str = "text"
    required: bool = False
    options: list[str] = field(default_factory=list)
    autocomplete: str = ""
    identifier: str = ""
    placeholder: str = ""
    nearby_text: str = ""

    def signature(self) -> str:
        raw = " ".join(
            [
                self.label,
                self.placeholder,
                self.autocomplete,
                self.identifier,
            ]
        ).lower()
        raw = re.sub(r"[^a-z0-9\s_]", " ", raw)
        # IDs como ``_r_1g_`` são voláteis (React) e não podem virar padrão de site.
        raw = re.sub(r"\b_[a-z0-9_]{2,}_", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        aliases = {
            "family name": "last name",
            "given name": "first name",
            "surname": "last name",
            "full name": "name",
            "e mail": "email",
            "mobile phone": "phone",
            "cell phone": "phone",
            "telephone": "phone",
            "currriculum vitae": "resume",
            "cv": "resume",
            "years of experience": "experience years",
        }
        for source, target in aliases.items():
            raw = raw.replace(source, target)
        if re.search(r"\bresume\b|\bcv\b|curr culo|curriculo", raw):
            raw = "resume"
        return raw
