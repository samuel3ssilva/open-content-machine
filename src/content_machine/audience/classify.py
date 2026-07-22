"""Deterministic, explainable role-family classification.

This is plain local code (docs/privacy.md rule 3: deterministic before
generative -- NO model calls). It maps a job-title string onto a coarse
role *family* using ordered, data-driven keyword tables. Every result carries
an explicit confidence level and a human-readable ``matched_evidence`` string
naming the exact rule/keyword that fired, so no classification is a black box.

Trust boundary: classification consumes ONLY the (already normalized) position
string. It never sees names, emails, URLs, or any other identifier, and the
evidence it emits is a rule/keyword from the tables below -- never a person's
data (docs/privacy.md rule 6).

Confidence policy
-----------------
- HIGH   -- a direct, unambiguous title match (e.g. "software engineer",
  "cfo", "product manager"): exactly one family matched at the high tier.
- MEDIUM -- probable but ambiguous: a single-token/high-tier match that maps
  cleanly but is weaker, OR a compound title whose high-tier keywords point at
  two different families (the first family by table priority is chosen).
- LOW    -- weak/incomplete evidence: a lone generic token ("analyst") or an
  abbreviation, with no domain to disambiguate.
- UNKNOWN -- empty title or no rule fired at all. The family is then ``unknown``
  and evidence is empty. Classification is NEVER forced: a title with zero
  evidence is left ``unknown`` rather than dumped into ``other``.

``other`` is reserved for clearly-recognized professions that are simply not in
the listed families (e.g. "physician", "pilot"); it is only assigned when such
a keyword explicitly matches.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RoleFamily(StrEnum):
    """Coarse professional family a title maps to. String-valued."""

    founder_executive = "founder_executive"
    engineering_data_ai = "engineering_data_ai"
    product = "product"
    marketing_growth_content = "marketing_growth_content"
    sales_bd_partnerships = "sales_bd_partnerships"
    design_ux = "design_ux"
    operations_people_finance_legal = "operations_people_finance_legal"
    education_research = "education_research"
    other = "other"
    unknown = "unknown"


class Confidence(StrEnum):
    """Explicit confidence level attached to a classification. String-valued."""

    high = "high"
    medium = "medium"
    low = "low"
    unknown = "unknown"


class RoleClassification(BaseModel):
    """Result of classifying one job title. ``matched_evidence`` is always a
    rule/keyword name (never personal data) and is non-empty whenever
    ``family`` is not ``unknown``."""

    model_config = ConfigDict(extra="forbid")

    family: RoleFamily = RoleFamily.unknown
    confidence: Confidence = Confidence.unknown
    matched_evidence: str = ""


# --- Keyword tables -------------------------------------------------------
#
# Each tier is an ordered tuple of ``(family, keywords)``. Order is the table
# *priority*: when a compound title matches two families at the same tier, the
# earlier family wins (with medium confidence). Keep the tables the single
# source of truth -- rules must stay explainable.
#
# A keyword containing any non-alphanumeric character (space, slash, hyphen) is
# matched as a plain casefolded substring; a bare alphabetic token is matched on
# word boundaries so "lead" does not fire inside "leadership".

_Tier = tuple[tuple[RoleFamily, tuple[str, ...]], ...]

# HIGH: direct, unambiguous title signals.
_HIGH: _Tier = (
    (
        RoleFamily.founder_executive,
        (
            "founder",
            "co-founder",
            "cofounder",
            "co founder",
            "owner",
            "proprietor",
            "ceo",
            "cto",
            "cfo",
            "coo",
            "cmo",
            "ciso",
            "cio",
            "cpo",
            "chro",
            "cro",
            "chief",
            "managing director",
            "executive director",
            "managing partner",
            "board member",
        ),
    ),
    (
        RoleFamily.engineering_data_ai,
        (
            "software engineer",
            "software developer",
            "web developer",
            "mobile developer",
            "ios developer",
            "android developer",
            "data engineer",
            "data scientist",
            "data analyst",
            "analytics engineer",
            "machine learning",
            "ml engineer",
            "ai engineer",
            "deep learning",
            "computer vision",
            "prompt engineer",
            "backend",
            "back-end",
            "back end",
            "frontend",
            "front-end",
            "front end",
            "full stack",
            "full-stack",
            "fullstack",
            "devops",
            "dev ops",
            "sre",
            "site reliability",
            "platform engineer",
            "infrastructure engineer",
            "cloud engineer",
            "systems engineer",
            "security engineer",
            "qa engineer",
            "test engineer",
            "solutions architect",
            "software architect",
            "data architect",
            "programmer",
        ),
    ),
    (
        RoleFamily.product,
        (
            "product manager",
            "product owner",
            "product lead",
            "head of product",
            "director of product",
            "chief product officer",
            "product management",
            "group product manager",
            "technical product manager",
        ),
    ),
    (
        RoleFamily.design_ux,
        (
            "ux designer",
            "ui designer",
            "ux/ui",
            "ui/ux",
            "product designer",
            "graphic designer",
            "visual designer",
            "brand designer",
            "web designer",
            "motion designer",
            "interaction designer",
            "user experience",
            "user research",
            "ux researcher",
            "design lead",
            "head of design",
            "design director",
            "creative director",
        ),
    ),
    (
        RoleFamily.marketing_growth_content,
        (
            "marketing",
            "growth",
            "content marketing",
            "product marketing",
            "content strategist",
            "content creator",
            "copywriter",
            "copywriting",
            "social media",
            "community manager",
            "brand manager",
            "seo",
            "demand generation",
            "public relations",
            "communications",
        ),
    ),
    (
        RoleFamily.sales_bd_partnerships,
        (
            "sales",
            "account executive",
            "account manager",
            "account director",
            "business development",
            "biz dev",
            "bizdev",
            "partnerships",
            "partner manager",
            "sales representative",
            "sales rep",
            "customer success",
            "client partner",
        ),
    ),
    (
        RoleFamily.operations_people_finance_legal,
        (
            "operations",
            "financial controller",
            "financial analyst",
            "controller",
            "accountant",
            "accounting",
            "bookkeeper",
            "human resources",
            "people operations",
            "people ops",
            "recruiter",
            "recruiting",
            "talent acquisition",
            "general counsel",
            "legal counsel",
            "attorney",
            "lawyer",
            "paralegal",
            "compliance",
            "procurement",
            "supply chain",
            "project manager",
            "program manager",
            "chief of staff",
            "executive assistant",
            "office manager",
        ),
    ),
    (
        RoleFamily.education_research,
        (
            "professor",
            "lecturer",
            "teacher",
            "instructor",
            "educator",
            "researcher",
            "research scientist",
            "research fellow",
            "postdoc",
            "post-doc",
            "principal investigator",
            "teaching assistant",
            "phd candidate",
        ),
    ),
    (
        RoleFamily.other,
        (
            "physician",
            "surgeon",
            "nurse",
            "dentist",
            "pharmacist",
            "veterinarian",
            "pilot",
            "chef",
            "electrician",
            "plumber",
            "carpenter",
            "firefighter",
            "police officer",
            "social worker",
            "real estate agent",
            "realtor",
        ),
    ),
)

# MEDIUM: probable but ambiguous single-token signals. Only consulted when no
# high-tier rule fired.
_MEDIUM: _Tier = (
    (
        RoleFamily.founder_executive,
        (
            "president",
            "vice president",
            "vp",
            "director",
            "head of",
            "general manager",
            "partner",
        ),
    ),
    (RoleFamily.engineering_data_ai, ("engineer", "developer", "architect")),
    (RoleFamily.product, ("product",)),
    (RoleFamily.design_ux, ("designer", "design")),
    (RoleFamily.operations_people_finance_legal, ("operations", "finance", "legal", "risk")),
    (RoleFamily.education_research, ("research", "academic")),
)

# LOW: weak/incomplete evidence -- a lone generic token or abbreviation.
_LOW: _Tier = (
    (RoleFamily.engineering_data_ai, ("analyst", "data")),
    (RoleFamily.operations_people_finance_legal, ("coordinator", "administrator")),
    (RoleFamily.marketing_growth_content, ("writer", "editor")),
)


def _compile(tier: _Tier) -> tuple[tuple[RoleFamily, str, re.Pattern[str] | None], ...]:
    """Flatten a tier into ordered ``(family, keyword, pattern)`` rules.

    ``pattern`` is a compiled word-boundary regex for bare alphabetic tokens, or
    ``None`` when the keyword should be matched as a plain substring.
    """
    rules: list[tuple[RoleFamily, str, re.Pattern[str] | None]] = []
    for family, keywords in tier:
        for kw in keywords:
            if kw.isalpha():
                pattern = re.compile(rf"(?<![a-z]){re.escape(kw)}(?![a-z])")
                rules.append((family, kw, pattern))
            else:
                rules.append((family, kw, None))
    return tuple(rules)


_HIGH_RULES = _compile(_HIGH)
_MEDIUM_RULES = _compile(_MEDIUM)
_LOW_RULES = _compile(_LOW)


def _matches(text: str, keyword: str, pattern: re.Pattern[str] | None) -> bool:
    if pattern is not None:
        return pattern.search(text) is not None
    return keyword in text


def _evidence(keyword: str, family: RoleFamily, *, compound: bool = False) -> str:
    base = f"keyword '{keyword}' -> {family.value}"
    if compound:
        return base + " (compound title; first family by table priority, medium confidence)"
    return base


def classify_role(position: str | None) -> RoleClassification:
    """Classify a job title into a :class:`RoleFamily` with confidence + evidence.

    Pure and deterministic: the same input always yields the same output,
    independent of any surrounding processing order. Consumes only the position
    string. Returns ``family=unknown`` (never forced) when the title is empty or
    no rule matches.
    """
    if not position or not position.strip():
        return RoleClassification()

    text = " ".join(position.casefold().split())

    # HIGH tier: gather every high match in table-priority order.
    high_hits: list[tuple[RoleFamily, str]] = []
    for family, keyword, pattern in _HIGH_RULES:
        if _matches(text, keyword, pattern):
            high_hits.append((family, keyword))

    if high_hits:
        distinct = {fam for fam, _ in high_hits}
        family, keyword = high_hits[0]
        if len(distinct) == 1:
            return RoleClassification(
                family=family,
                confidence=Confidence.high,
                matched_evidence=_evidence(keyword, family),
            )
        # Compound title spanning >=2 families -> medium, first by priority.
        return RoleClassification(
            family=family,
            confidence=Confidence.medium,
            matched_evidence=_evidence(keyword, family, compound=True),
        )

    # MEDIUM tier.
    for family, keyword, pattern in _MEDIUM_RULES:
        if _matches(text, keyword, pattern):
            return RoleClassification(
                family=family,
                confidence=Confidence.medium,
                matched_evidence=_evidence(keyword, family),
            )

    # LOW tier.
    for family, keyword, pattern in _LOW_RULES:
        if _matches(text, keyword, pattern):
            return RoleClassification(
                family=family,
                confidence=Confidence.low,
                matched_evidence=_evidence(keyword, family),
            )

    return RoleClassification()
