"""Testes das regras de preenchimento sem inventar dados."""
from src.core.config import Settings
from src.execution.forms.field import FormField
from src.execution.forms.mapper import resolve_local
from src.models.domain import AnswerRule, CandidateProfile, PersonalInfo


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


def test_human_evidence_resolves_work_authorization():
    settings = Settings.load()
    profile = CandidateProfile(
        answer_rules=[
            AnswerRule(
                match=r"right to work|autoriza[cç][aã]o de trabalho",
                value="Cidadania brasileira",
                category="human_evidence",
            )
        ]
    )
    valor, origem, precisa_humano, categoria = resolve_local(
        FormField(
            label="Please confirm the basis of your right to work in the country",
            field_type="text",
        ),
        profile,
        settings,
    )
    assert valor == "Cidadania brasileira"
    assert origem == "profile.human_evidence"
    assert not precisa_humano
    assert categoria == "general"


def test_human_evidence_resolves_referral_question():
    settings = Settings.load()
    profile = CandidateProfile(
        answer_rules=[
            AnswerRule(
                match=r"related to anyone who works at capco",
                value="No",
                category="human_evidence",
            )
        ]
    )
    valor, origem, precisa_humano, _ = resolve_local(
        FormField(
            label="Do you know anyone or are you related to anyone who works at Capco?",
            field_type="radio",
        ),
        profile,
        settings,
    )
    assert valor == "No"
    assert origem == "profile.human_evidence"
    assert not precisa_humano


def test_recruitcrm_placeholder_labels_use_rendered_context():
    settings = Settings.load()
    profile = CandidateProfile(
        personal=PersonalInfo(
            phone_display="+55 (11) 95305-8585",
            linkedin="https://www.linkedin.com/in/addamreis",
            location="São Paulo, SP, Brasil",
        ),
        years_experience=7,
        expectations={"salary": "A combinar"},
        answer_rules=[
            AnswerRule(
                match="(autoriz|i agree|i accept).*(termos|terms|privacidade|privacy)",
                value=True,
            ),
            AnswerRule(match="how did you hear", value="LinkedIn"),
        ],
    )
    casos = [
        (
            FormField(
                label="+1-202-555-0194",
                placeholder="+1-202-555-0194",
                identifier="sTest-candidateMobile",
                nearby_text="Phone*",
            ),
            "+55 (11) 95305-8585",
        ),
        (
            FormField(
                label="radix-_r_n_",
                field_type="combobox",
                identifier="radix-_r_o_",
                nearby_text="City of Residence*",
            ),
            "São Paulo",
        ),
        (
            FormField(
                label="_r_p_-form-item",
                field_type="combobox",
                identifier="sTest-custColumnInputDropdownField",
                nearby_text="Country* Not Selected",
            ),
            "Brazil",
        ),
        (
            FormField(
                label="Write your answer",
                identifier="sTest-candidateAnswer",
                nearby_text=(
                    "How many years of relevant experience do you have for this role?"
                ),
            ),
            "7",
        ),
        (
            FormField(
                label="Write your answer",
                identifier="sTest-candidateAnswer",
                nearby_text="What is you salary expectation? (USD Monthly)",
            ),
            "A combinar",
        ),
        (
            FormField(
                label="I agree to Candidate Terms",
                field_type="checkbox",
                identifier="candidateTermsBlock",
            ),
            "True",
        ),
    ]
    for campo, esperado in casos:
        valor, _origem, precisa_humano, _categoria = resolve_local(
            campo, profile, settings
        )
        assert not precisa_humano, campo.label
        assert valor == esperado, campo.label


def test_neighbor_labels_cannot_hijack_field_resolution():
    """Mesmo com o contexto ruidoso antigo, cada campo usa o próprio rótulo."""
    settings = Settings.load()
    profile = CandidateProfile(
        personal=PersonalInfo(
            first_name="Addam",
            last_name="Oliveira",
            email="addamreis@gmail.com",
            phone_display="+55 (11) 95305-8585",
            location="São Paulo, SP, Brasil",
        ),
        years_experience=7,
        expectations={"salario": "A combinar"},
    )
    ruido = "First Name* Email* Phone* Country* Not Selected"
    casos = [
        (FormField(label="Tony", identifier="firstName", nearby_text=ruido), "Addam"),
        (
            FormField(
                label="Country* Not Selected",
                identifier="country",
                nearby_text=ruido,
            ),
            "Brazil",
        ),
        (
            FormField(
                label="Write your answer",
                identifier="answer",
                nearby_text=(
                    "How many years of relevant experience do you have for this role? "
                    "What is you salary expectation? (USD Monthly)"
                ),
            ),
            "7",
        ),
    ]
    for campo, esperado in casos:
        valor, origem, humano, _ = resolve_local(campo, profile, settings)
        assert not humano, campo.label
        assert valor == esperado, (campo.label, origem)


def test_vetsez_work_authorization_requires_human_even_with_short_label():
    settings = Settings.load()
    profile = CandidateProfile()
    campo = FormField(
        label="NoYes",
        field_type="select",
        required=True,
        options=["No", "Yes"],
        identifier="section_1783434304566_question_1",
        nearby_text=(
            "Are you authorized to work legally in the United States?*"
            "NoYesA response is required"
        ),
    )
    valor, _origem, precisa_humano, _categoria = resolve_local(
        campo, profile, settings
    )
    assert valor == ""
    assert precisa_humano
