"""Testes das regras de preenchimento sem inventar dados."""
from src.core.config import Settings
from src.execution.forms.field import FormField
from src.execution.forms.mapper import resolve_local
from src.models.domain import CandidateProfile, PersonalInfo


def test_email_resolves_from_profile():
    settings = Settings.load()
    profile = CandidateProfile(
        personal=PersonalInfo(email="addamreis@gmail.com"),
        documents={"resume": "C:/cv.pdf"},
    )
    value, source, needs_human, _ = resolve_local(
        FormField(label="Email address", field_type="text"), profile, settings
    )
    assert value == "addamreis@gmail.com"
    assert not needs_human
    assert "email" in source


def test_country_code_select_uses_profile_location():
    settings = Settings.load()
    profile = CandidateProfile(personal=PersonalInfo(location="São Paulo, SP, Brasil"))
    value, source, needs_human, _ = resolve_local(
        FormField(
            label="Código do país",
            field_type="select",
            options=["Andorra (+376)", "Brasil (+55)", "Canadá (+1)"],
        ),
        profile,
        settings,
    )
    assert value == "Brasil (+55)"
    assert "location" in source
    assert not needs_human


def test_resume_radio_with_filename_resolves_to_profile_cv():
    settings = Settings.load()
    profile = CandidateProfile(documents={"resume": "C:/cv-internacional.pdf"})
    value, source, needs_human, category = resolve_local(
        FormField(
            label="Addam dos Reis Oliveira - Software Engineer.pdf",
            field_type="radio",
        ),
        profile,
        settings,
    )
    assert value == "C:/cv-internacional.pdf"
    assert not needs_human
    assert category == "resume"
    assert "resume" in source


def test_legal_terms_never_auto_accepted():
    settings = Settings.load()
    profile = CandidateProfile()
    _, _, needs_human, category = resolve_local(
        FormField(label="Aceito os termos e politica de privacidade", field_type="checkbox"),
        profile,
        settings,
    )
    assert needs_human
    assert category == "sensitive"
