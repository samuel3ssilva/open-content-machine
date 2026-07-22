"""Deterministic, explainable role-*family* classification.

This is plain local code (docs/privacy.md rule 3: deterministic before
generative -- NO model calls). It maps a job-title string onto a coarse role
*family* (a professional FUNCTION) using ordered, data-driven keyword tables.
Every result carries an explicit confidence level and a human-readable
``matched_evidence`` string naming the exact *tier + rule* that fired, so no
classification is a black box.

Trust boundary: classification consumes ONLY the (already normalized) position
string. It never sees names, emails, URLs, or any other identifier, and the
evidence it emits is a tier/keyword from the tables below -- never a person's
data (docs/privacy.md rule 6).

Family vs seniority (ticket OPUS-1.1 §1)
----------------------------------------
Role *family* is the professional FUNCTION (engineering, marketing, ...).
*Seniority* is the LEVEL (head, director, manager, VP, ...) and is derived
separately in :func:`content_machine.audience.normalize.infer_seniority` from
the SAME normalized title. A seniority word must NEVER, on its own, assign a
family. In particular ``founder_executive`` is RESERVED for general executive
leadership / ownership (founder, co-founder, owner, CEO, president as general
leadership, managing partner, general/managing director without a functional
qualifier) -- it is *not* where "Director of Engineering" or "Head of Product"
land. Those keep their FUNCTION (engineering, product) with a director/head
seniority extracted independently.

Precedence tiers (ticket OPUS-1.1 §2)
-------------------------------------
Evidence is evaluated in this fixed, documented order; the first tier that
produces any hit wins, and no later tier can override it:

  T0  ownership override      -- founder/co-founder/owner tokens. Ownership is
                                 the defining role, so it dominates BOTH family
                                 (founder_executive) and seniority (founder_owner)
                                 even in a compound like "Founder & CTO" (§6).
  T1  exact/phrase functional -- multi-word functional phrases and C-suite
                                 acronyms mapped to their function (CTO ->
                                 engineering, CFO -> operations..., CMO ->
                                 marketing). Compound titles spanning >=2
                                 functional families resolve to the highest-
                                 priority family, confidence downgraded to
                                 medium.
  T2  strong domain keywords  -- single unambiguous functional tokens
                                 (engineering, marketing, vendas, design...).
                                 This is where functional director/head titles
                                 land: "Director of Engineering" -> engineering
                                 via the "engineering" token, never founder.
  T3  recognized professions  -- clearly-recognized professions outside the
                                 listed families (physician, pilot...) -> other.
  T4  general executive/owner  -- CEO, president (as leadership), managing/
                                 general/executive director/manager, managing
                                 partner, board member -> founder_executive.
                                 Only reached when NO functional evidence fired,
                                 so a functional qualifier always wins.
  T5  weak/ambiguous          -- lone generic tokens (analyst, consultant,
                                 partner-without-ownership...) at low confidence.
  T6  unknown                 -- nothing matched, or a seniority-only title.

Ties within a functional tier are resolved by the documented family priority
(:data:`_FAMILY_PRIORITY`) with confidence downgraded to medium.

Confidence policy
-----------------
- HIGH    -- an unambiguous functional match (one family at T1/T2), a recognized
  profession (T3), or an unambiguous executive term (CEO at T4).
- MEDIUM  -- a compound functional title (>=2 families), a deliberately hedged
  functional rule (e.g. "sales engineer" -> sales), or an ambiguous-level
  executive term (managing director, president alone).
- LOW     -- weak/incomplete evidence (a lone generic token or abbreviation).
- UNKNOWN -- empty title or no rule fired. The family is then ``unknown`` and
  evidence is empty. Classification is NEVER forced: a title with zero evidence,
  or a seniority-only title, is left ``unknown`` rather than dumped elsewhere.

``other`` is reserved for clearly-recognized professions not in the listed
families (T3, high) and for recognized-but-non-functional terms such as bare
"consultant"/"partner" (T5, low); it is only assigned when such a keyword
explicitly matches.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from content_machine.audience.normalize import normalize_title


class RoleFamily(StrEnum):
    """Coarse professional family (FUNCTION) a title maps to. String-valued."""

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
    """Result of classifying one job title. ``matched_evidence`` names the tier
    and rule that fired (never personal data) and is non-empty whenever
    ``family`` is not ``unknown``."""

    model_config = ConfigDict(extra="forbid")

    family: RoleFamily = RoleFamily.unknown
    confidence: Confidence = Confidence.unknown
    matched_evidence: str = ""


# --- Family priority ------------------------------------------------------
#
# Used to break ties within a functional tier (a compound title spanning >=2
# families resolves to the highest-priority family, medium confidence). Earlier
# = higher priority. Engineering precedes product so "Data Scientist and Product
# Manager" -> engineering (documented, matches the legacy behavior).
_FAMILY_PRIORITY: tuple[RoleFamily, ...] = (
    RoleFamily.founder_executive,
    RoleFamily.engineering_data_ai,
    RoleFamily.product,
    RoleFamily.design_ux,
    RoleFamily.marketing_growth_content,
    RoleFamily.sales_bd_partnerships,
    RoleFamily.operations_people_finance_legal,
    RoleFamily.education_research,
    RoleFamily.other,
)
_PRIORITY_INDEX: dict[RoleFamily, int] = {
    fam: i for i, fam in enumerate(_FAMILY_PRIORITY)
}

# A rule is (family, keyword, confidence). ``keyword`` is matched against the
# normalized title: a bare alphabetic token matches on word boundaries (so
# "lead" does not fire inside "leadership"); anything containing a space is
# matched as a plain substring.
_Rule = tuple[RoleFamily, str, Confidence]

_F = RoleFamily
_C = Confidence

# --- T0: ownership override ------------------------------------------------
# Ownership is the defining role and dominates everything else (§6). First match
# wins; always founder_executive / high. A bare "partner"/"socio" is NOT here
# (see T5) -- only explicit founder/owner tokens. ``forbidden`` names substrings
# that veto a rule: the bare "owner" token is vetoed inside functional "X owner"
# titles (product/process/service/scrum owner) where "owner" denotes role
# stewardship, not company ownership -- those fall through to their function.
_T0Rule = tuple[str, tuple[str, ...]]
_T0_OWNERSHIP: tuple[_T0Rule, ...] = (
    ("founder", ()),
    ("co founder", ()),
    ("cofounder", ()),
    ("fundador", ()),
    ("fundadora", ()),
    ("cofundador", ()),
    ("cofundadora", ()),
    ("co fundador", ()),
    ("owner", ("product owner", "process owner", "service owner", "scrum owner")),
    ("proprietor", ()),
    ("proprietario", ()),
    ("proprietaria", ()),
    # Sprint 1.1: PT "dono/dona" (colloquial "owner"). Mirrors the "owner"
    # false-friend guard above -- "Dono de Produto/Processo/Serviço" is a PT
    # rendering of product/process/service owner (role stewardship), NOT
    # company ownership, so it must fall through to its function instead.
    (
        "dono",
        (
            "dono de produto",
            "dono do produto",
            "dono de processo",
            "dono do processo",
            "dono de servico",
            "dono do servico",
        ),
    ),
    (
        "dona",
        (
            "dona de produto",
            "dona do produto",
            "dona de processo",
            "dona do processo",
            "dona de servico",
            "dona do servico",
        ),
    ),
)

# --- T1: exact / phrase-level functional matches ---------------------------
# Multi-word functional phrases and C-suite acronyms mapped to their FUNCTION.
# Gathered together; a single family -> that rule's confidence, multiple
# families -> compound (highest priority, medium). Deliberately hedged rules
# carry medium (e.g. "sales engineer" is a pre-sales function, not core eng).
_T1_FUNCTIONAL_PHRASES: tuple[_Rule, ...] = (
    # engineering / data / AI
    (_F.engineering_data_ai, "software engineer", _C.high),
    (_F.engineering_data_ai, "software developer", _C.high),
    (_F.engineering_data_ai, "web developer", _C.high),
    (_F.engineering_data_ai, "mobile developer", _C.high),
    (_F.engineering_data_ai, "ios developer", _C.high),
    (_F.engineering_data_ai, "android developer", _C.high),
    (_F.engineering_data_ai, "data engineer", _C.high),
    (_F.engineering_data_ai, "data scientist", _C.high),
    (_F.engineering_data_ai, "data analyst", _C.high),
    # Phrase-level BI evidence (bare "bi" stays unmapped -- too ambiguous):
    (_F.engineering_data_ai, "analista de bi", _C.high),
    (_F.engineering_data_ai, "bi analyst", _C.high),
    (_F.engineering_data_ai, "analytics engineer", _C.high),
    (_F.engineering_data_ai, "machine learning engineer", _C.high),
    (_F.engineering_data_ai, "machine learning", _C.high),
    (_F.engineering_data_ai, "artificial intelligence", _C.high),
    (_F.engineering_data_ai, "inteligencia artificial", _C.high),
    (_F.engineering_data_ai, "deep learning", _C.high),
    (_F.engineering_data_ai, "computer vision", _C.high),
    (_F.engineering_data_ai, "prompt engineer", _C.high),
    (_F.engineering_data_ai, "platform engineer", _C.high),
    (_F.engineering_data_ai, "infrastructure engineer", _C.high),
    (_F.engineering_data_ai, "cloud engineer", _C.high),
    (_F.engineering_data_ai, "systems engineer", _C.high),
    (_F.engineering_data_ai, "security engineer", _C.high),
    (_F.engineering_data_ai, "qa engineer", _C.high),
    (_F.engineering_data_ai, "test engineer", _C.high),
    (_F.engineering_data_ai, "site reliability", _C.high),
    (_F.engineering_data_ai, "solutions architect", _C.high),
    (_F.engineering_data_ai, "software architect", _C.high),
    (_F.engineering_data_ai, "data architect", _C.high),
    (_F.engineering_data_ai, "back end", _C.high),
    (_F.engineering_data_ai, "front end", _C.high),
    (_F.engineering_data_ai, "full stack", _C.high),
    (_F.engineering_data_ai, "cientista de dados", _C.high),
    (_F.engineering_data_ai, "engenheiro de software", _C.high),
    (_F.engineering_data_ai, "engenheiro de dados", _C.high),
    (_F.engineering_data_ai, "arquiteto de software", _C.high),
    (_F.engineering_data_ai, "arquiteto de solucoes", _C.high),
    (_F.engineering_data_ai, "chief technology officer", _C.high),
    (_F.engineering_data_ai, "chief information officer", _C.high),
    (_F.engineering_data_ai, "chief information security officer", _C.high),
    (_F.engineering_data_ai, "chief data officer", _C.high),
    (_F.engineering_data_ai, "cto", _C.high),
    (_F.engineering_data_ai, "cio", _C.high),
    (_F.engineering_data_ai, "ciso", _C.high),
    # Sprint 1.1: broaden PT/EN engineering phrase coverage (ticket §1).
    (_F.engineering_data_ai, "engenharia de software", _C.high),
    (_F.engineering_data_ai, "engenharia de dados", _C.high),
    (_F.engineering_data_ai, "engenheira de software", _C.high),
    (_F.engineering_data_ai, "engenheira de dados", _C.high),
    (_F.engineering_data_ai, "arquiteta de software", _C.high),
    (_F.engineering_data_ai, "arquiteta de solucoes", _C.high),
    (_F.engineering_data_ai, "arquiteto de dados", _C.high),
    (_F.engineering_data_ai, "arquiteta de dados", _C.high),
    (_F.engineering_data_ai, "arquiteto de cloud", _C.high),
    (_F.engineering_data_ai, "arquiteta de cloud", _C.high),
    (_F.engineering_data_ai, "desenvolvedor full stack", _C.high),
    (_F.engineering_data_ai, "desenvolvedora full stack", _C.high),
    (_F.engineering_data_ai, "desenvolvedor backend", _C.high),
    (_F.engineering_data_ai, "desenvolvedora backend", _C.high),
    (_F.engineering_data_ai, "desenvolvedor frontend", _C.high),
    (_F.engineering_data_ai, "desenvolvedora frontend", _C.high),
    (_F.engineering_data_ai, "desenvolvedor mobile", _C.high),
    (_F.engineering_data_ai, "desenvolvedora mobile", _C.high),
    (_F.engineering_data_ai, "ciencia de dados", _C.high),
    (_F.engineering_data_ai, "aprendizado de maquina", _C.high),
    (_F.engineering_data_ai, "business intelligence", _C.high),
    (_F.engineering_data_ai, "inteligencia de negocios", _C.high),
    (_F.engineering_data_ai, "quality assurance", _C.high),
    (_F.engineering_data_ai, "seguranca da informacao", _C.high),
    (_F.engineering_data_ai, "information security", _C.high),
    # "CDO" is ambiguous (Chief Data Officer vs Chief Digital Officer); both
    # readings are technology-adjacent for this audience, so it maps here at
    # medium rather than high (documented in docs/classification.md).
    (_F.engineering_data_ai, "cdo", _C.medium),
    # "Scrum Master"/"Agile Coach" are delivery-facilitation roles, not core
    # engineering, but are overwhelmingly embedded in software delivery teams
    # in this dataset's target audience -- hedged to medium, same pattern as
    # "sales engineer" below (documented in docs/classification.md).
    (_F.engineering_data_ai, "scrum master", _C.medium),
    (_F.engineering_data_ai, "agile coach", _C.medium),
    # "Tech Lead" is a common, unambiguous mixed-language title (ticket §2's
    # "Tech Lead na StartupX" example) -- safe as a 2-word phrase since it
    # cannot fire on unrelated "tech"-prefixed words (fintech, biotech, ...).
    (_F.engineering_data_ai, "tech lead", _C.high),
    # product
    (_F.product, "product manager", _C.high),
    (_F.product, "product owner", _C.high),
    (_F.product, "product lead", _C.high),
    (_F.product, "head of product", _C.high),
    (_F.product, "director of product", _C.high),
    (_F.product, "chief product officer", _C.high),
    (_F.product, "product management", _C.high),
    (_F.product, "group product manager", _C.high),
    (_F.product, "technical product manager", _C.high),
    (_F.product, "gerente de produto", _C.high),
    (_F.product, "gerente de produtos", _C.high),
    (_F.product, "coordenador de produto", _C.high),
    (_F.product, "coordenadora de produto", _C.high),
    (_F.product, "analista de produto", _C.high),
    # "CPO" is ambiguous (Chief Product Officer vs Chief People Officer vs
    # Chief Privacy Officer); mapped to product at medium given this app's
    # tech/startup audience skews toward the product reading (documented in
    # docs/classification.md). Note "po" is deliberately NOT added bare
    # (Purchase Order / too ambiguous) -- "product owner" above already
    # covers the unambiguous phrase.
    (_F.product, "cpo", _C.medium),
    # design / UX  (designer outranks a product/brand modifier -> phrase first)
    (_F.design_ux, "product designer", _C.high),
    (_F.design_ux, "ux designer", _C.high),
    (_F.design_ux, "ui designer", _C.high),
    (_F.design_ux, "ux ui", _C.high),
    (_F.design_ux, "ui ux", _C.high),
    (_F.design_ux, "graphic designer", _C.high),
    (_F.design_ux, "visual designer", _C.high),
    (_F.design_ux, "brand designer", _C.high),
    (_F.design_ux, "web designer", _C.high),
    (_F.design_ux, "motion designer", _C.high),
    (_F.design_ux, "interaction designer", _C.high),
    (_F.design_ux, "user experience", _C.high),
    (_F.design_ux, "user research", _C.high),
    (_F.design_ux, "ux researcher", _C.high),
    (_F.design_ux, "design lead", _C.high),
    (_F.design_ux, "head of design", _C.high),
    (_F.design_ux, "design director", _C.high),
    (_F.design_ux, "creative director", _C.high),
    (_F.design_ux, "designer de produto", _C.high),
    (_F.design_ux, "experiencia do usuario", _C.high),
    (_F.design_ux, "designer grafico", _C.high),
    (_F.design_ux, "designer de servico", _C.high),
    (_F.design_ux, "pesquisa com usuarios", _C.high),
    (_F.design_ux, "ux writer", _C.high),
    (_F.design_ux, "service design", _C.high),
    # marketing / growth / content
    (_F.marketing_growth_content, "content marketing", _C.high),
    (_F.marketing_growth_content, "product marketing", _C.high),
    (_F.marketing_growth_content, "growth marketing", _C.high),
    (_F.marketing_growth_content, "content strategist", _C.high),
    (_F.marketing_growth_content, "content creator", _C.high),
    (_F.marketing_growth_content, "social media", _C.high),
    (_F.marketing_growth_content, "midias sociais", _C.high),
    (_F.marketing_growth_content, "community manager", _C.high),
    (_F.marketing_growth_content, "brand manager", _C.high),
    (_F.marketing_growth_content, "demand generation", _C.high),
    (_F.marketing_growth_content, "public relations", _C.high),
    (_F.marketing_growth_content, "marketing de conteudo", _C.high),
    (_F.marketing_growth_content, "gerente de marketing", _C.high),
    (_F.marketing_growth_content, "cmo", _C.high),
    (_F.marketing_growth_content, "redes sociais", _C.high),
    (_F.marketing_growth_content, "comunicacao corporativa", _C.high),
    (_F.marketing_growth_content, "relacoes publicas", _C.high),
    (_F.marketing_growth_content, "growth hacker", _C.high),
    (_F.marketing_growth_content, "marketing de crescimento", _C.high),
    # sales / BD / partnerships
    (_F.sales_bd_partnerships, "account executive", _C.high),
    (_F.sales_bd_partnerships, "account manager", _C.high),
    (_F.sales_bd_partnerships, "account director", _C.high),
    (_F.sales_bd_partnerships, "business development", _C.high),
    (_F.sales_bd_partnerships, "partner manager", _C.high),
    (_F.sales_bd_partnerships, "sales representative", _C.high),
    (_F.sales_bd_partnerships, "sales rep", _C.high),
    (_F.sales_bd_partnerships, "customer success", _C.high),
    (_F.sales_bd_partnerships, "client partner", _C.high),
    (_F.sales_bd_partnerships, "inside sales", _C.high),
    (_F.sales_bd_partnerships, "pre sales", _C.high),
    (_F.sales_bd_partnerships, "executivo de contas", _C.high),
    (_F.sales_bd_partnerships, "desenvolvimento de negocios", _C.high),
    (_F.sales_bd_partnerships, "gerente de vendas", _C.high),
    (_F.sales_bd_partnerships, "representante comercial", _C.high),
    (_F.sales_bd_partnerships, "executiva de contas", _C.high),
    (_F.sales_bd_partnerships, "key account", _C.high),
    (_F.sales_bd_partnerships, "sucesso do cliente", _C.high),
    (_F.sales_bd_partnerships, "parcerias estrategicas", _C.high),
    # Spelled out, "Chief Revenue Officer" is unambiguous (literally names
    # "revenue"), unlike the bare "cro" acronym below -- same asymmetry as
    # "chief data officer" (high) vs bare "cdo" (medium).
    (_F.sales_bd_partnerships, "chief revenue officer", _C.high),
    # "Sales Engineer" is a pre-sales function -> sales, hedged to medium (§5).
    (_F.sales_bd_partnerships, "sales engineer", _C.medium),
    # "CRO" is genuinely ambiguous: Chief Revenue Officer (sales/BD) vs Chief
    # Risk Officer (finance/ops). Mapped here at MEDIUM (revenue reading,
    # documented in docs/classification.md) -- never high, since either
    # reading is plausible without more context.
    (_F.sales_bd_partnerships, "cro", _C.medium),
    # operations / people / finance / legal
    (_F.operations_people_finance_legal, "people operations", _C.high),
    (_F.operations_people_finance_legal, "people ops", _C.high),
    (_F.operations_people_finance_legal, "human resources", _C.high),
    (_F.operations_people_finance_legal, "recursos humanos", _C.high),
    (_F.operations_people_finance_legal, "talent acquisition", _C.high),
    (_F.operations_people_finance_legal, "general counsel", _C.high),
    (_F.operations_people_finance_legal, "legal counsel", _C.high),
    (_F.operations_people_finance_legal, "chief financial officer", _C.high),
    (_F.operations_people_finance_legal, "chief operating officer", _C.high),
    (_F.operations_people_finance_legal, "chief human resources officer", _C.high),
    (_F.operations_people_finance_legal, "chief people officer", _C.high),
    (_F.operations_people_finance_legal, "chief of staff", _C.high),
    (_F.operations_people_finance_legal, "financial controller", _C.high),
    (_F.operations_people_finance_legal, "financial analyst", _C.high),
    (_F.operations_people_finance_legal, "supply chain", _C.high),
    (_F.operations_people_finance_legal, "project manager", _C.high),
    (_F.operations_people_finance_legal, "program manager", _C.high),
    (_F.operations_people_finance_legal, "executive assistant", _C.high),
    (_F.operations_people_finance_legal, "office manager", _C.high),
    (_F.operations_people_finance_legal, "gerente de operacoes", _C.high),
    (_F.operations_people_finance_legal, "gerente de projetos", _C.high),
    (_F.operations_people_finance_legal, "cfo", _C.high),
    (_F.operations_people_finance_legal, "coo", _C.high),
    (_F.operations_people_finance_legal, "chro", _C.high),
    (_F.operations_people_finance_legal, "recrutamento e selecao", _C.high),
    (_F.operations_people_finance_legal, "folha de pagamento", _C.high),
    (_F.operations_people_finance_legal, "cadeia de suprimentos", _C.high),
    # education / research
    (_F.education_research, "research scientist", _C.high),
    (_F.education_research, "research fellow", _C.high),
    (_F.education_research, "principal investigator", _C.high),
    (_F.education_research, "teaching assistant", _C.high),
    (_F.education_research, "phd candidate", _C.high),
    (_F.education_research, "post doc", _C.high),
    (_F.education_research, "pesquisador cientifico", _C.high),
    (_F.education_research, "pos doutorado", _C.high),
)

# --- T2: strong single-token domain keywords -------------------------------
# Unambiguous functional tokens. This is where functional director/head/VP
# titles resolve to their FUNCTION (e.g. "Director of Engineering" via the
# "engineering" token). Gathered together like T1.
_T2_STRONG_DOMAIN: tuple[_Rule, ...] = (
    (_F.engineering_data_ai, "engineering", _C.high),
    (_F.engineering_data_ai, "engenharia", _C.high),
    (_F.engineering_data_ai, "developer", _C.high),
    (_F.engineering_data_ai, "desenvolvedor", _C.high),
    (_F.engineering_data_ai, "desenvolvedora", _C.high),
    (_F.engineering_data_ai, "programmer", _C.high),
    (_F.engineering_data_ai, "programador", _C.high),
    (_F.engineering_data_ai, "programadora", _C.high),
    (_F.engineering_data_ai, "devops", _C.high),
    (_F.engineering_data_ai, "sre", _C.high),
    (_F.engineering_data_ai, "backend", _C.high),
    (_F.engineering_data_ai, "frontend", _C.high),
    (_F.engineering_data_ai, "fullstack", _C.high),
    (_F.engineering_data_ai, "technology", _C.high),
    (_F.engineering_data_ai, "tecnologia", _C.high),
    (_F.engineering_data_ai, "data", _C.high),
    (_F.engineering_data_ai, "dados", _C.high),
    (_F.engineering_data_ai, "ai", _C.high),
    (_F.engineering_data_ai, "cybersecurity", _C.high),
    (_F.engineering_data_ai, "analytics", _C.high),
    (_F.engineering_data_ai, "qa", _C.high),
    (_F.engineering_data_ai, "infosec", _C.high),
    (_F.engineering_data_ai, "ciberseguranca", _C.high),
    (_F.product, "product", _C.high),
    (_F.product, "produto", _C.high),
    (_F.design_ux, "design", _C.high),
    (_F.design_ux, "designer", _C.high),
    (_F.design_ux, "ux", _C.high),
    (_F.design_ux, "ui", _C.high),
    (_F.design_ux, "ilustrador", _C.high),
    (_F.design_ux, "ilustradora", _C.high),
    (_F.design_ux, "illustrator", _C.high),
    (_F.marketing_growth_content, "marketing", _C.high),
    (_F.marketing_growth_content, "growth", _C.high),
    (_F.marketing_growth_content, "content", _C.high),
    (_F.marketing_growth_content, "conteudo", _C.high),
    (_F.marketing_growth_content, "comunicacao", _C.high),
    (_F.marketing_growth_content, "branding", _C.high),
    (_F.marketing_growth_content, "seo", _C.high),
    (_F.marketing_growth_content, "copywriting", _C.high),
    (_F.marketing_growth_content, "copywriter", _C.high),
    (_F.marketing_growth_content, "publicidade", _C.high),
    (_F.marketing_growth_content, "propaganda", _C.high),
    (_F.sales_bd_partnerships, "sales", _C.high),
    (_F.sales_bd_partnerships, "vendas", _C.high),
    (_F.sales_bd_partnerships, "comercial", _C.high),
    (_F.sales_bd_partnerships, "partnerships", _C.high),
    (_F.sales_bd_partnerships, "parcerias", _C.high),
    (_F.sales_bd_partnerships, "sdr", _C.high),
    (_F.sales_bd_partnerships, "bdr", _C.high),
    (_F.operations_people_finance_legal, "operations", _C.high),
    (_F.operations_people_finance_legal, "operacoes", _C.high),
    (_F.operations_people_finance_legal, "finance", _C.high),
    (_F.operations_people_finance_legal, "financeiro", _C.high),
    (_F.operations_people_finance_legal, "financial", _C.high),
    (_F.operations_people_finance_legal, "accounting", _C.high),
    (_F.operations_people_finance_legal, "contabil", _C.high),
    (_F.operations_people_finance_legal, "controller", _C.high),
    (_F.operations_people_finance_legal, "controladoria", _C.high),
    (_F.operations_people_finance_legal, "accountant", _C.high),
    (_F.operations_people_finance_legal, "recruiter", _C.high),
    (_F.operations_people_finance_legal, "recruiting", _C.high),
    (_F.operations_people_finance_legal, "recrutamento", _C.high),
    (_F.operations_people_finance_legal, "recrutador", _C.high),
    (_F.operations_people_finance_legal, "recrutadora", _C.high),
    (_F.operations_people_finance_legal, "talent", _C.high),
    (_F.operations_people_finance_legal, "hr", _C.high),
    (_F.operations_people_finance_legal, "juridico", _C.high),
    (_F.operations_people_finance_legal, "legal", _C.high),
    (_F.operations_people_finance_legal, "attorney", _C.high),
    (_F.operations_people_finance_legal, "lawyer", _C.high),
    (_F.operations_people_finance_legal, "advogado", _C.high),
    (_F.operations_people_finance_legal, "advogada", _C.high),
    (_F.operations_people_finance_legal, "paralegal", _C.high),
    (_F.operations_people_finance_legal, "compliance", _C.high),
    (_F.operations_people_finance_legal, "procurement", _C.high),
    (_F.operations_people_finance_legal, "logistica", _C.high),
    (_F.operations_people_finance_legal, "compras", _C.high),
    # "Qualidade" (PT "quality") maps to ops rather than engineering: bare PT
    # usage overwhelmingly means manufacturing/process quality (e.g. "Analista
    # de Qualidade"), not software QA -- software QA is already covered
    # explicitly via "qa"/"quality assurance"/"qa engineer" above (documented
    # in docs/classification.md).
    (_F.operations_people_finance_legal, "qualidade", _C.high),
    (_F.operations_people_finance_legal, "contabilidade", _C.high),
    (_F.operations_people_finance_legal, "tesouraria", _C.high),
    (_F.operations_people_finance_legal, "auditoria", _C.high),
    (_F.operations_people_finance_legal, "tributario", _C.high),
    (_F.operations_people_finance_legal, "advocacia", _C.high),
    (_F.operations_people_finance_legal, "regulatorio", _C.high),
    (_F.operations_people_finance_legal, "pmo", _C.high),
    (_F.education_research, "professor", _C.high),
    (_F.education_research, "professora", _C.high),
    (_F.education_research, "docente", _C.high),
    (_F.education_research, "researcher", _C.high),
    (_F.education_research, "research", _C.high),
    (_F.education_research, "lecturer", _C.high),
    (_F.education_research, "pesquisador", _C.high),
    (_F.education_research, "pesquisadora", _C.high),
    (_F.education_research, "academic", _C.high),
    (_F.education_research, "academico", _C.high),
    (_F.education_research, "academica", _C.high),
    (_F.education_research, "postdoc", _C.high),
    (_F.education_research, "ensino", _C.high),
    (_F.education_research, "educacao", _C.high),
    (_F.education_research, "pedagogia", _C.high),
    (_F.education_research, "pedagogo", _C.high),
    (_F.education_research, "pedagoga", _C.high),
    (_F.education_research, "doutorando", _C.high),
    (_F.education_research, "doutoranda", _C.high),
    (_F.education_research, "mestrando", _C.high),
    (_F.education_research, "mestranda", _C.high),
)

# --- T3: recognized non-listed professions -> other (high) -----------------
_T3_PROFESSIONS: tuple[str, ...] = (
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
    "medico",
    "medica",
    "enfermeiro",
    "enfermeira",
    "piloto",
    # Sprint 1.1 additions. "Jornalista" (journalist) is deliberately routed
    # here (other), not marketing_growth_content: pure journalism is a
    # distinct recognized profession, whereas content/social-media roles stay
    # in marketing (documented in docs/classification.md). "Arquiteto(a)"
    # (building architect) is checked only after T1's "arquiteto de
    # software/solucoes/dados/cloud" phrases, so "Arquiteto de Software"
    # still resolves to engineering at T1 -- this bare form only catches a
    # context-free "Arquiteto" (buildings).
    "jornalista",
    "psicologo",
    "psicologa",
    "nutricionista",
    "arquiteto",
    "arquiteta",
)

# --- T4: general executive / ownership-lite -> founder_executive -----------
# Only reached when no functional evidence fired. ``forbidden`` names substrings
# that veto a rule: "president"/"presidente" must not fire on "vice president"
# (that is a seniority-only VP, family unknown). First match wins.
_T4Rule = tuple[str, Confidence, tuple[str, ...]]
_T4_EXECUTIVE: tuple[_T4Rule, ...] = (
    ("chief executive officer", _C.high, ()),
    ("ceo", _C.high, ()),
    ("managing partner", _C.high, ()),
    ("managing director", _C.medium, ()),
    ("executive director", _C.medium, ()),
    ("general manager", _C.medium, ()),
    ("board member", _C.medium, ()),
    ("chairman", _C.medium, ()),
    ("chairwoman", _C.medium, ()),
    ("chairperson", _C.medium, ()),
    ("presidente", _C.medium, ("vice",)),
    ("president", _C.medium, ("vice",)),
    ("chief", _C.medium, ()),
    ("diretor executivo", _C.medium, ()),
    ("diretora executiva", _C.medium, ()),
    # "Empreendedor(a)" (entrepreneur, PT) is weaker evidence than an explicit
    # founder/owner claim (T0) -- it is a self-identifier that does not always
    # denote a formal ownership stake -- so it lands here at medium rather
    # than in T0 (documented in docs/classification.md).
    ("empreendedor", _C.medium, ()),
    ("empreendedora", _C.medium, ()),
)

# --- T5: weak / ambiguous -> low -------------------------------------------
# Lone generic tokens. Recognized-but-non-functional terms (consultant, and a
# partner/socio WITHOUT ownership evidence) map to ``other`` low rather than
# being forced into a function or into founder_executive (§5). Order matters:
# "risk" -> operations is listed before "analyst" -> engineering so a "Risk
# Analyst" reads as operations, not engineering.
_T5_WEAK: tuple[_Rule, ...] = (
    (_F.other, "consultant", _C.low),
    (_F.other, "consultor", _C.low),
    (_F.other, "consultora", _C.low),
    (_F.other, "partner", _C.low),
    (_F.other, "socio", _C.low),
    (_F.operations_people_finance_legal, "risk", _C.low),
    (_F.operations_people_finance_legal, "risco", _C.low),
    # NOTE (Fable review, Sprint 1.1): bare "coordinator/coordenador" is a
    # seniority word, not a function, and bare "analyst/analista" is
    # cross-domain (finance/ops/marketing as often as data). Mapping them to a
    # family here was unjustified inference; they now fall through to unknown.
    # Seniority extraction still buckets them (manager_lead /
    # individual_contributor) independently.
    (_F.operations_people_finance_legal, "administrator", _C.low),
    (_F.operations_people_finance_legal, "administrador", _C.low),
    (_F.marketing_growth_content, "writer", _C.low),
    (_F.marketing_growth_content, "editor", _C.low),
    (_F.marketing_growth_content, "redator", _C.low),
    (_F.engineering_data_ai, "engineer", _C.low),
    (_F.engineering_data_ai, "engenheiro", _C.low),
    (_F.engineering_data_ai, "engenheira", _C.low),
)


def _matches(text: str, keyword: str) -> bool:
    """Match ``keyword`` against normalized ``text``.

    A bare alphabetic token matches on word boundaries; anything containing a
    space (a phrase) matches as a plain substring.
    """
    if keyword.isalpha():
        return re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", text) is not None
    return keyword in text


def _resolve_functional(
    text: str, rules: tuple[_Rule, ...], tier_label: str
) -> RoleClassification | None:
    """Gather every hit in ``rules`` and resolve a functional tier.

    One distinct family -> that family with the first matching rule's confidence.
    Two or more distinct families -> the highest-priority family (compound),
    confidence downgraded to medium.
    """
    hits: list[_Rule] = [r for r in rules if _matches(text, r[1])]
    if not hits:
        return None
    families = {r[0] for r in hits}
    if len(families) == 1:
        family, keyword, confidence = hits[0]
        return RoleClassification(
            family=family,
            confidence=confidence,
            matched_evidence=f"{tier_label}: '{keyword}' -> {family.value}",
        )
    winner = min(families, key=lambda f: _PRIORITY_INDEX[f])
    keyword = next(r[1] for r in hits if r[0] is winner)
    ordered = ", ".join(sorted(f.value for f in families))
    return RoleClassification(
        family=winner,
        confidence=Confidence.medium,
        matched_evidence=(
            f"{tier_label}: '{keyword}' -> {winner.value} "
            f"(compound: {{{ordered}}}; first by priority, medium)"
        ),
    )


def classify_role(position: str | None) -> RoleClassification:
    """Classify a job title into a :class:`RoleFamily` with confidence + evidence.

    Pure and deterministic: the same input always yields the same output,
    independent of any surrounding processing order. Consumes only the position
    string (matched via the shared :func:`normalize_title` pre-pass). Returns
    ``family=unknown`` (never forced) when the title is empty, is seniority-only,
    or no rule matches. See the module docstring for the full tier precedence.
    """
    text = normalize_title(position)
    if not text:
        return RoleClassification()

    # T0: ownership override -- dominates all functional/executive evidence.
    for keyword, forbidden in _T0_OWNERSHIP:
        if forbidden and any(bad in text for bad in forbidden):
            continue
        if _matches(text, keyword):
            return RoleClassification(
                family=RoleFamily.founder_executive,
                confidence=Confidence.high,
                matched_evidence=(
                    f"tier0_ownership: '{keyword}' -> {RoleFamily.founder_executive.value}"
                ),
            )

    # T1: exact/phrase functional matches (with compound resolution).
    result = _resolve_functional(text, _T1_FUNCTIONAL_PHRASES, "tier1_functional_phrase")
    if result is not None:
        return result

    # T2: strong single-token domain keywords (with compound resolution).
    result = _resolve_functional(text, _T2_STRONG_DOMAIN, "tier2_strong_domain")
    if result is not None:
        return result

    # T3: recognized non-listed professions -> other (high), first match.
    for keyword in _T3_PROFESSIONS:
        if _matches(text, keyword):
            return RoleClassification(
                family=RoleFamily.other,
                confidence=Confidence.high,
                matched_evidence=(
                    f"tier3_recognized_profession: '{keyword}' -> {RoleFamily.other.value}"
                ),
            )

    # T4: general executive / ownership-lite -> founder_executive, first match.
    for keyword, confidence, forbidden in _T4_EXECUTIVE:
        if forbidden and any(bad in text for bad in forbidden):
            continue
        if _matches(text, keyword):
            return RoleClassification(
                family=RoleFamily.founder_executive,
                confidence=confidence,
                matched_evidence=(
                    f"tier4_general_executive: '{keyword}' -> "
                    f"{RoleFamily.founder_executive.value}"
                ),
            )

    # T5: weak/ambiguous lone tokens -> low, first match.
    for family, keyword, confidence in _T5_WEAK:
        if _matches(text, keyword):
            return RoleClassification(
                family=family,
                confidence=confidence,
                matched_evidence=f"tier5_weak_ambiguous: '{keyword}' -> {family.value}",
            )

    # T6: nothing matched (or seniority-only title) -> unknown, never forced.
    return RoleClassification()
