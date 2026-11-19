"""Normalização de localização de vagas.

O card do LinkedIn pode trazer só cidade/UF (``Manaus, AM``), só país
(``Lisbon, Portugal``) ou só região (``LATAM``). Esta função grava sempre que
possível ``País - informação original`` para que a análise saiba se a vaga é
brasileira ou internacional sem depender de heurística na hora de decidir.
"""
from __future__ import annotations

import re

# Países com alias explícitos no texto da vaga.
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "Brazil": ("brazil", "brasil"),
    "United States": ("united states", "united states of america", "usa", "u.s.", "u.s.a"),
    "Portugal": ("portugal",),
    "United Kingdom": ("united kingdom", "uk", "england", "scotland", "wales"),
    "Germany": ("germany", "deutschland"),
    "France": ("france",),
    "Netherlands": ("netherlands", "holland"),
    "Spain": ("spain", "españa"),
    "Italy": ("italy", "italia"),
    "Switzerland": ("switzerland", "suíça", "suica"),
    "Canada": ("canada",),
    "Australia": ("australia",),
    "New Zealand": ("new zealand", "nova zelandia"),
    "Ireland": ("ireland",),
    "Mexico": ("mexico", "méxico"),
    "India": ("india",),
    "Latin America": ("latin america", "latam", "américa latina", "america latina"),
    "Europe": ("europe", "europa"),
    "Sweden": ("sweden", "suécia", "suezia"),
    "Finland": ("finland", "finlândia"),
    "Norway": ("norway", "noruega"),
    "Denmark": ("denmark", "dinamarca"),
    "Poland": ("poland", "polônia", "polonia"),
    "Austria": ("austria", "áustria"),
    "Belgium": ("belgium", "bélgica"),
    "Czech Republic": ("czech republic", "czechia"),
    "Greece": ("greece", "grécia"),
    "Hungary": ("hungary", "hungria"),
    "Romania": ("romania", "romênia"),
    "Ukraine": ("ukraine", "ucrânia"),
    "Israel": ("israel",),
    "United Arab Emirates": ("united arab emirates", "uae"),
    "Singapore": ("singapore",),
    "Japan": ("japan", "japão"),
    "China": ("china",),
    "South Korea": ("south korea", "korea", "coreia do sul"),
    "Argentina": ("argentina",),
    "Chile": ("chile",),
    "Colombia": ("colombia", "colômbia"),
    "Peru": ("peru", "perú"),
    "Uruguay": ("uruguay",),
    "Venezuela": ("venezuela",),
    "Costa Rica": ("costa rica",),
    "Panama": ("panama", "panamá"),
    "South Africa": ("south africa", "áfrica do sul"),
    "Egypt": ("egypt", "egito"),
    "Turkey": ("turkey", "turquia"),
    "Vietnam": ("vietnam", "vietnã"),
    "Bulgaria": ("bulgaria", "bulgária"),
}

# UFs e códigos de estado EUA: comparados com case-sensitive para não casar
# palavras comuns como "in", "or", "am".
_BRAZIL_UFS = {
    "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms",
    "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc",
    "se", "sp", "to",
}
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi",
    "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn",
    "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh",
    "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa",
    "wv", "wi", "wy",
}

# Cidades conhecidas (mais específicas primeiro para não confundir
# "Porto Alegre/RS" com "Porto/Portugal").
_CITY_COUNTRY: tuple[tuple[str, str], ...] = (
    ("porto alegre", "Brazil"),
    ("rio de janeiro", "Brazil"),
    ("são paulo", "Brazil"),
    ("sao paulo", "Brazil"),
    ("belo horizonte", "Brazil"),
    ("florianópolis", "Brazil"),
    ("florianopolis", "Brazil"),
    ("brasília", "Brazil"),
    ("brasilia", "Brazil"),
    ("goiânia", "Brazil"),
    ("goiania", "Brazil"),
    ("vitória", "Brazil"),
    ("vitoria", "Brazil"),
    ("belém", "Brazil"),
    ("belem", "Brazil"),
    ("manaus", "Brazil"),
    ("salvador", "Brazil"),
    ("recife", "Brazil"),
    ("curitiba", "Brazil"),
    ("fortaleza", "Brazil"),
    ("campinas", "Brazil"),
    ("bethesda", "United States"),
    ("silver spring", "United States"),
    ("san francisco", "United States"),
    ("new york", "United States"),
    ("los angeles", "United States"),
    ("palo alto", "United States"),
    ("mountain view", "United States"),
    ("miami", "United States"),
    ("boston", "United States"),
    ("chicago", "United States"),
    ("seattle", "United States"),
    ("austin", "United States"),
    ("washington", "United States"),
    ("lisbon", "Portugal"),
    ("lisboa", "Portugal"),
    ("porto", "Portugal"),
    ("braga", "Portugal"),
    ("coimbra", "Portugal"),
    ("funchal", "Portugal"),
    ("berlin", "Germany"),
    ("munich", "Germany"),
    ("hamburg", "Germany"),
    ("london", "United Kingdom"),
    ("manchester", "United Kingdom"),
    ("dublin", "Ireland"),
    ("paris", "France"),
    ("amsterdam", "Netherlands"),
    ("madrid", "Spain"),
    ("barcelona", "Spain"),
    ("milan", "Italy"),
    ("zurich", "Switzerland"),
    ("toronto", "Canada"),
    ("vancouver", "Canada"),
    ("montreal", "Canada"),
    ("sydney", "Australia"),
    ("melbourne", "Australia"),
    ("auckland", "New Zealand"),
    ("mexico city", "Mexico"),
    ("cambridge", "United States"),
    ("rome", "Italy"),
    ("stockholm", "Sweden"),
    ("helsinki", "Finland"),
    ("oslo", "Norway"),
    ("copenhagen", "Denmark"),
    ("warsaw", "Poland"),
    ("vienna", "Austria"),
    ("brussels", "Belgium"),
    ("prague", "Czech Republic"),
    ("athens", "Greece"),
    ("budapest", "Hungary"),
    ("bucharest", "Romania"),
    ("kyiv", "Ukraine"),
    ("tel aviv", "Israel"),
    ("singapore", "Singapore"),
    ("tokyo", "Japan"),
    ("beijing", "China"),
    ("shanghai", "China"),
    ("seoul", "South Korea"),
    ("buenos aires", "Argentina"),
    ("santiago", "Chile"),
    ("bogotá", "Colombia"),
    ("bogota", "Colombia"),
    ("lima", "Peru"),
    ("montevideo", "Uruguay"),
    ("caracas", "Venezuela"),
    ("san josé", "Costa Rica"),
    ("san jose", "Costa Rica"),
    ("panama city", "Panama"),
    ("cape town", "South Africa"),
    ("cairo", "Egypt"),
    ("istanbul", "Turkey"),
    ("dubai", "United Arab Emirates"),
    ("abu dhabi", "United Arab Emirates"),
    # Cidades que desempatam UFs compartilhadas com estados dos EUA (SC/MA/PA).
    ("blumenau", "Brazil"),
    ("santos", "Brazil"),
    ("jaguariúna", "Brazil"),
    ("jaguariuna", "Brazil"),
    ("são bernardo do campo", "Brazil"),
    ("sao bernardo do campo", "Brazil"),
    ("fort mill", "United States"),
    ("mansfield", "United States"),
    ("pittsburgh", "United States"),
    ("charlotte", "United States"),
    ("austin", "United States"),
    ("dallas", "United States"),
    ("houston", "United States"),
    ("madison", "United States"),
    ("covington", "United States"),
    ("des moines", "United States"),
    ("san antonio", "United States"),
    ("west lafayette", "United States"),
    ("belfast", "United Kingdom"),
    ("agrate brianza", "Italy"),
    ("bologna", "Italy"),
    ("cagliari", "Italy"),
    ("turin", "Italy"),
    ("bordeaux", "France"),
    ("brest", "France"),
    ("courbevoie", "France"),
    ("lille", "France"),
    ("st.-denis", "France"),
    ("dresden", "Germany"),
    ("mannheim", "Germany"),
    ("heide", "Germany"),
    ("amersfoort", "Netherlands"),
    ("utrecht", "Netherlands"),
    ("chicoutimi", "Canada"),
    ("halifax", "Canada"),
    ("calgary", "Canada"),
    ("ottawa", "Canada"),
    ("hanoi", "Vietnam"),
    ("sofia", "Bulgaria"),
    ("fully", "Switzerland"),
    ("unterentfelden", "Switzerland"),
    ("san sebastián de los reyes", "Spain"),
    ("cerdanyola del vallès", "Spain"),
    ("wellington", "New Zealand"),
)

# Nomes exibidos na coluna normalizada (o detect_country continua com chaves
# canônicas em inglês para não quebrar a lógica de região).
_DISPLAY_NAME: dict[str, str] = {
    "Brazil": "Brasil",
    "United States": "Estados Unidos",
    "United Kingdom": "Reino Unido",
    "Latin America": "América Latina",
    "Europe": "Europa",
    "Germany": "Alemanha",
    "France": "França",
    "Netherlands": "Países Baixos",
    "Spain": "Espanha",
    "Italy": "Itália",
    "Switzerland": "Suíça",
    "Canada": "Canadá",
    "New Zealand": "Nova Zelândia",
    "Ireland": "Irlanda",
    "Mexico": "México",
    "Sweden": "Suécia",
    "Finland": "Finlândia",
    "Norway": "Noruega",
    "Denmark": "Dinamarca",
    "Poland": "Polônia",
    "Austria": "Áustria",
    "Belgium": "Bélgica",
    "Czech Republic": "República Tcheca",
    "Greece": "Grécia",
    "Hungary": "Hungria",
    "Romania": "Romênia",
    "Ukraine": "Ucrânia",
    "United Arab Emirates": "Emirados Árabes Unidos",
    "Japan": "Japão",
    "China": "China",
    "South Korea": "Coreia do Sul",
    "Colombia": "Colômbia",
    "Peru": "Peru",
    "Costa Rica": "Costa Rica",
    "Panama": "Panamá",
    "South Africa": "África do Sul",
    "Egypt": "Egito",
    "Turkey": "Turquia",
    "Vietnam": "Vietnã",
    "Bulgaria": "Bulgária",
}

# UFs/estados são gravados em maiúsculas ("Manaus, AM", "Bethesda, MD").
# Tokens exatos em maiúsculas evitam casar "in", "or" ou "am" minúsculos.
_UPPER_TOKEN_RE = re.compile(r"\b[A-Z]{2}\b")
# Âncora primeiro: caso contrário ``\s{2,}`` consome só os espaços e deixa o
# hífen de uma localização já normalizada ("United States - Bethesda MD").
_CLEAN_RE = re.compile(r"^[\s,\-–]+|[\s,\-–]+$|\s{2,}")
_DANGLING_COMMA_RE = re.compile(r",\s*(?=[(\[])")


def detect_country(location: str) -> str:
    """Retorna o país/região inferido, ou ``''`` quando não há informação."""
    raw = (location or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    for country, aliases in _COUNTRY_NAMES.items():
        for alias in aliases:
            if len(alias) <= 4 and alias.isalpha():
                if re.search(rf"\b{re.escape(alias)}\b", lowered, re.IGNORECASE):
                    return country
            elif alias in lowered:
                return country

    # Cidade conhecida resolve ambiguidade de UF/EUA: "Cambridge, MA" é MA
    # (Massachusetts), enquanto "São Luís, MA" é Maranhão.
    for city, country in _CITY_COUNTRY:
        if re.search(rf"\b{re.escape(city)}\b", lowered, re.IGNORECASE):
            return country
    upper_tokens = {token.lower() for token in _UPPER_TOKEN_RE.findall(raw)}
    if upper_tokens & _BRAZIL_UFS:
        return "Brazil"
    if upper_tokens & _US_STATES:
        return "United States"
    return ""


def normalize_location(location: str) -> str:
    """Devolve ``País - informação da vaga`` quando o país é conhecido."""
    raw = (location or "").strip()
    if not raw:
        return ""
    # Já normalizado: não duplica o prefixo no retrofill.
    for display in _DISPLAY_NAME.values():
        if raw.lower().startswith(f"{display.lower()} - "):
            return raw
    country = detect_country(raw)
    if not country:
        return raw

    rest = raw
    for alias in _COUNTRY_NAMES.get(country, ()):
        rest = re.sub(rf"\b{re.escape(alias)}\b", " ", rest, flags=re.IGNORECASE)
    rest = _DANGLING_COMMA_RE.sub(" ", rest)
    rest = _CLEAN_RE.sub("", rest).strip()
    label = _DISPLAY_NAME.get(country, country)
    return f"{label} - {rest}" if rest else label
