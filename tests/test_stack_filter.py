"""Pré-filtro determinístico de stack, compartilhado por discovery e classificação."""
from src.shared.stack_filter import (
    is_off_stack_title,
    mentions_dotnet,
    mentions_frontend,
    mentions_java,
)


def test_java_is_not_confused_with_javascript() -> None:
    assert mentions_java("Senior Java Developer")
    assert mentions_java("Java 17 + Spring Boot")
    assert not mentions_java("Senior JavaScript Developer")
    assert not mentions_java("Node.js/TypeScript Engineer")


def test_frontend_signals() -> None:
    assert mentions_frontend("Frontend Developer")
    assert mentions_frontend("Front-end Engineer")
    assert mentions_frontend("ReactJS Developer")
    assert mentions_frontend("Senior Vue.js Developer")
    assert mentions_frontend("UI Engineer")
    assert not mentions_frontend(".NET Engineer")


def test_focused_titles_are_off_stack() -> None:
    assert is_off_stack_title("Senior Java Developer")
    assert is_off_stack_title("Backend Java Engineer")
    assert is_off_stack_title("Senior Frontend Developer")
    assert is_off_stack_title("React Developer")
    assert is_off_stack_title("Angular Developer")
    assert not is_off_stack_title("")
    assert not is_off_stack_title("Senior .NET Engineer")


def test_dotnet_titles_are_protected() -> None:
    """Java/React complementares em vaga .NET não podem ser pré-descartados."""
    assert not is_off_stack_title(".NET Developer with Java integration")
    assert not is_off_stack_title("Senior .NET Developer, React front end")
    assert not is_off_stack_title("Full Stack Developer (ReactJS + .NET C# WebAPI)")
    assert not is_off_stack_title("C# Engineer with Vue.js")
