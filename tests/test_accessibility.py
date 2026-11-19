"""Testes sem dispositivo para leitura da árvore de acessibilidade."""
from src.execution.android.accessibility import (
    extract_form_fields,
    extract_job_cards,
    extract_job_description,
    parse_posted,
)

XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node class="android.widget.FrameLayout" clickable="true" resource-id="job-card-1">
    <node class="android.widget.TextView" text="Senior Backend Engineer"/>
    <node class="android.widget.TextView" text="Example Company"/>
    <node class="android.widget.TextView" text="Remote - United States"/>
    <node class="android.widget.TextView" text="$130,000 - $170,000 a year"/>
    <node class="android.widget.TextView" text="2 days ago"/>
  </node>
  <node class="android.widget.FrameLayout" clickable="true" resource-id="job-card-2">
    <node class="android.widget.TextView" text="Software Engineer"/>
    <node class="android.widget.TextView" text="Other Company"/>
    <node class="android.widget.TextView" text="São Paulo, Brazil"/>
    <node class="android.widget.TextView" text="5h ago"/>
  </node>
</hierarchy>
"""


def test_extract_job_cards():
    cards = extract_job_cards(XML)
    assert len(cards) == 2
    assert cards[0].title == "Senior Backend Engineer"
    assert cards[0].company == "Example Company"
    assert cards[0].posted_precision == "day"
    assert cards[1].posted_precision == "hour"


REAL_LINKEDIN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" clickable="false" resource-id="">
    <node class="android.widget.View" clickable="true" bounds="[0,570][1080,852]">
      <node class="android.widget.TextView" text="Staff Software Engineer" content-desc="Staff Software Engineer"/>
      <node class="android.widget.TextView" text="bp"/>
      <node class="android.widget.TextView" text="São Paulo, SP (Hybrid)"/>
      <node class="android.widget.TextView" text="7 company alumni work here"/>
      <node class="android.widget.TextView" text="1 week ago" content-desc="Posted 1 week ago"/>
      <node class="android.widget.View" content-desc="Dismiss Staff Software Engineer job"/>
    </node>
    <node class="android.widget.View" clickable="true" bounds="[0,855][1080,1161]">
      <node class="android.widget.View" content-desc="Desenvolvedor Back-end Sr | .NET (Verified job)"/>
      <node class="android.widget.TextView" text="Webmotors"/>
      <node class="android.widget.TextView" text="São Paulo, SP (Hybrid)"/>
      <node class="android.widget.TextView" text="5 company alumni work here"/>
      <node class="android.widget.TextView" text="4 days ago" content-desc="Posted 4 days ago"/>
      <node class="android.widget.View" content-desc="Dismiss Desenvolvedor Back-end Sr | .NET job"/>
    </node>
  </node>
</hierarchy>
"""


def test_extract_real_linkedin_cards_from_content_desc():
    cards = extract_job_cards(REAL_LINKEDIN_XML)
    assert len(cards) == 2
    assert cards[0].title == "Staff Software Engineer"
    assert cards[0].company == "bp"
    assert cards[0].location == "São Paulo, SP (Hybrid)"
    assert cards[0].posted_raw == "1 week ago"
    assert cards[0].posted_precision == "week"
    assert cards[1].title == "Desenvolvedor Back-end Sr | .NET"
    assert cards[1].company == "Webmotors"
    assert cards[1].posted_precision == "day"


def test_apply_marker_is_captured_from_card():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" clickable="false">
        <node class="android.widget.View" clickable="true">
          <node class="android.widget.View" content-desc="Backend Engineer (Verified job)"/>
          <node class="android.widget.TextView" text="Acme"/>
          <node class="android.widget.TextView" text="Brazil (Remote)"/>
          <node class="android.widget.TextView" text="4h ago" content-desc="Posted 4h ago"/>
          <node class="android.widget.TextView" text="Easy Apply"/>
          <node class="android.widget.View" content-desc="Dismiss Backend Engineer job"/>
        </node>
        <node class="android.widget.View" clickable="true">
          <node class="android.widget.View" content-desc="Full Stack Engineer (Verified job)"/>
          <node class="android.widget.TextView" text="Other Co"/>
          <node class="android.widget.TextView" text="Brazil (Remote)"/>
          <node class="android.widget.TextView" text="5h ago" content-desc="Posted 5h ago"/>
          <node class="android.widget.TextView" text="Apply on company website"/>
          <node class="android.widget.View" content-desc="Dismiss Full Stack Engineer job"/>
        </node>
      </node>
    </hierarchy>"""
    cards = extract_job_cards(xml)
    assert len(cards) == 2
    assert cards[0].apply_marker == "easy apply"
    assert cards[1].apply_marker == "apply on company"


def test_card_without_posted_does_not_absorb_page_header():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" clickable="false">
        <node class="android.widget.TextView" text="99+ results"/>
        <node class="android.widget.TextView" text="Location Brazil"/>
        <node class="android.widget.View" clickable="true">
          <node class="android.widget.View" content-desc="Principal Full-Stack Developer (.Net+Angular) (Verified job)"/>
          <node class="android.widget.TextView" text="Luxoft"/>
          <node class="android.widget.TextView" text="Brazil (Remote)"/>
          <node class="android.widget.View" content-desc="Dismiss Principal Full-Stack Developer (.Net+Angular) job"/>
        </node>
      </node>
    </hierarchy>"""
    cards = extract_job_cards(xml)
    assert len(cards) == 1
    assert cards[0].company == "Luxoft"
    assert cards[0].location == "Brazil (Remote)"
    assert cards[0].posted_precision == "unknown"


def test_posted_precision_is_not_assumed():
    posted, precision = parse_posted("2 weeks ago")
    assert precision == "week"
    assert posted is not None
    assert parse_posted("publicado recentemente")[1] == "unknown"


def test_extract_job_description_from_accessibility_tree():
    detail_xml = """<hierarchy>
      <node class="android.widget.TextView" text="Senior Backend Engineer"/>
      <node class="android.widget.LinearLayout" resource-id="com.linkedin.android:id/job_description">
        <node class="android.widget.TextView" text="Responsavel por APIs .NET em AWS com PostgreSQL e mensageria."/>
        <node class="android.widget.TextView" text="Requer 7 anos de experiencia e ingles intermediario."/>
      </node>
    </hierarchy>"""
    description = extract_job_description(detail_xml)
    assert ".NET" in description
    assert "7 anos" in description


def test_extract_form_fields():
    form_xml = """<hierarchy>
      <node class="android.widget.EditText" text="Email" resource-id="email"/>
      <node class="android.widget.CheckBox" text="Aceito os termos de privacidade"/>
    </hierarchy>"""
    fields = extract_form_fields(form_xml)
    assert [field.field_type for field in fields] == ["text", "checkbox"]
