"""Tests for deterministic role-family classification."""

from __future__ import annotations

import pytest

from content_machine.audience.classify import (
    Confidence,
    RoleClassification,
    RoleFamily,
    classify_role,
)
from content_machine.audience.normalize import infer_seniority

# At least three positive, unambiguous (HIGH) cases per listed family. NOTE:
# founder_executive is RESERVED for general leadership/ownership (ticket
# OPUS-1.1 §1) -- functional C-suite titles like CFO/CTO/CMO map to their
# FUNCTION, not here.
_FAMILY_POSITIVE_CASES: dict[RoleFamily, list[str]] = {
    RoleFamily.founder_executive: ["Founder", "Chief Executive Officer", "CEO"],
    RoleFamily.engineering_data_ai: [
        "Software Engineer",
        "Data Engineer",
        "Machine Learning Engineer",
    ],
    RoleFamily.product: [
        "Product Manager",
        "Product Lead",
        "Group Product Manager",
    ],
    RoleFamily.marketing_growth_content: [
        "Marketing Manager",
        "Content Strategist",
        "Growth Lead",
    ],
    RoleFamily.sales_bd_partnerships: [
        "Account Executive",
        "Business Development Manager",
        "Head of Partnerships",
    ],
    RoleFamily.design_ux: ["UX Designer", "Graphic Designer", "Product Designer"],
    RoleFamily.operations_people_finance_legal: [
        "Financial Controller",
        "Recruiter",
        "General Counsel",
    ],
    RoleFamily.education_research: ["Professor", "Research Scientist", "Lecturer"],
    RoleFamily.other: ["Physician", "Pilot", "Nurse"],
}


@pytest.mark.parametrize("family", list(_FAMILY_POSITIVE_CASES))
def test_each_family_has_three_positive_high_cases(family: RoleFamily) -> None:
    cases = _FAMILY_POSITIVE_CASES[family]
    assert len(cases) >= 3
    for title in cases:
        result = classify_role(title)
        assert result.family is family, f"{title!r} -> {result.family}"
        assert result.confidence is Confidence.high
        assert result.matched_evidence


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "CFO",
        "Product Manager",
        "UX Designer",
        "Marketing Manager",
        "",
        None,
        "   ",
        "asdf qwerty zzz",
    ],
)
def test_determinism_repeated_runs(title: str | None) -> None:
    first = classify_role(title)
    second = classify_role(title)
    assert first == second


def test_determinism_independent_of_ordering() -> None:
    titles = [
        "Software Engineer",
        "Consultant",
        "CFO",
        "",
        "Marketing Intern",
        "asdf",
    ]
    forward = [classify_role(t) for t in titles]
    backward = list(reversed([classify_role(t) for t in reversed(titles)]))
    assert forward == backward


@pytest.mark.parametrize("title", ["", "   ", None, "asdfqwerty", "1234 zzzz"])
def test_empty_or_garbage_is_unknown_never_forced(title: str | None) -> None:
    result = classify_role(title)
    assert result.family is RoleFamily.unknown
    assert result.confidence is Confidence.unknown
    assert result.matched_evidence == ""


def test_ambiguous_lone_token_is_low() -> None:
    # A bare family-specific token with no qualifier -> weak evidence, low
    # confidence. (Bare "Analyst" is cross-domain and stays unknown -- see
    # test_bare_analyst_and_coordinator_are_unknown_family.)
    result = classify_role("Engineer")
    assert result.confidence is Confidence.low
    assert result.family is RoleFamily.engineering_data_ai
    assert result.matched_evidence


@pytest.mark.parametrize("title", ["Director", "Head", "Gerente", "VP", "Vice President"])
def test_seniority_only_title_has_unknown_family(title: str) -> None:
    # A bare seniority word is a LEVEL, not a FUNCTION -- family must be unknown
    # (ticket OPUS-1.1 §1/§5). The seniority itself is still extractable.
    result = classify_role(title)
    assert result.family is RoleFamily.unknown
    assert result.confidence is Confidence.unknown
    assert result.matched_evidence == ""
    assert infer_seniority(title) != "unknown"


# --- Family / seniority independence: the core CEO-mandated correction ------
# family = FUNCTION, seniority = LEVEL, parsed independently from one title.
_INDEPENDENCE_CASES: list[tuple[str, RoleFamily, str]] = [
    ("Director of Engineering", RoleFamily.engineering_data_ai, "vp_head_director"),
    ("Head of Product", RoleFamily.product, "vp_head_director"),
    ("Marketing Director", RoleFamily.marketing_growth_content, "vp_head_director"),
    ("Sales Manager", RoleFamily.sales_bd_partnerships, "manager_lead"),
    ("CTO", RoleFamily.engineering_data_ai, "c_level"),
    ("Chief Technology Officer", RoleFamily.engineering_data_ai, "c_level"),
    ("CMO", RoleFamily.marketing_growth_content, "c_level"),
    ("CFO", RoleFamily.operations_people_finance_legal, "c_level"),
    ("CEO", RoleFamily.founder_executive, "c_level"),
    ("Chief Executive Officer", RoleFamily.founder_executive, "c_level"),
    ("Diretor de Engenharia", RoleFamily.engineering_data_ai, "vp_head_director"),
    ("Diretor de Marketing", RoleFamily.marketing_growth_content, "vp_head_director"),
    ("Gerente de Vendas", RoleFamily.sales_bd_partnerships, "manager_lead"),
]


@pytest.mark.parametrize(("title", "family", "seniority"), _INDEPENDENCE_CASES)
def test_family_and_seniority_are_independent(
    title: str, family: RoleFamily, seniority: str
) -> None:
    result = classify_role(title)
    assert result.family is family, f"{title!r} -> {result.family}"
    assert infer_seniority(title) == seniority, title


@pytest.mark.parametrize(
    "title",
    [
        "Director of Engineering",
        "Head of Product",
        "Marketing Director",
        "Diretor de Tecnologia",
        "Head of Data",
        "VP of Marketing",
        "Managing Director, Technology",
    ],
)
def test_functional_leadership_never_founder_executive(title: str) -> None:
    # The canonical cross-domain error: a functional director/head/VP title must
    # NEVER be classified as founder_executive.
    assert classify_role(title).family is not RoleFamily.founder_executive


# --- §5 negative & ambiguity rules, each with a dedicated assertion ---------


def test_product_designer_is_design_not_product() -> None:
    # "designer" outranks the "product" modifier.
    assert classify_role("Product Designer").family is RoleFamily.design_ux


def test_sales_engineer_is_sales_medium() -> None:
    result = classify_role("Sales Engineer")
    assert result.family is RoleFamily.sales_bd_partnerships
    assert result.confidence is Confidence.medium


def test_people_operations_is_ops_with_people_ops_evidence() -> None:
    result = classify_role("People Operations")
    assert result.family is RoleFamily.operations_people_finance_legal
    assert "people operations" in result.matched_evidence


def test_managing_director_with_functional_qualifier_wins() -> None:
    assert (
        classify_role("Managing Director, Technology").family
        is RoleFamily.engineering_data_ai
    )


def test_consultant_without_domain_is_other_low() -> None:
    result = classify_role("Consultant")
    assert result.family is RoleFamily.other
    assert result.confidence is Confidence.low


def test_bare_partner_is_not_founder_executive() -> None:
    result = classify_role("Partner")
    assert result.family is not RoleFamily.founder_executive
    assert result.family is RoleFamily.other
    assert result.confidence is Confidence.low


def test_managing_partner_is_founder_executive() -> None:
    assert classify_role("Managing Partner").family is RoleFamily.founder_executive


def test_product_owner_is_product_not_ownership() -> None:
    # "owner" here is role stewardship, not company ownership.
    result = classify_role("Product Owner")
    assert result.family is RoleFamily.product


@pytest.mark.parametrize("title", ["Founder & CTO", "CTO & Co-founder"])
def test_founder_plus_function_is_founder_executive(title: str) -> None:
    # Ownership dominates BOTH family and seniority in a founder+function compound.
    assert classify_role(title).family is RoleFamily.founder_executive
    assert infer_seniority(title) == "founder_owner"


def test_compound_title_two_families_is_medium_first_by_priority() -> None:
    # "data scientist" (engineering, high) + "product manager" (product, high).
    result = classify_role("Data Scientist and Product Manager")
    assert result.confidence is Confidence.medium
    # Engineering has higher table priority than product.
    assert result.family is RoleFamily.engineering_data_ai
    assert "compound" in result.matched_evidence


def test_evidence_nonempty_whenever_family_known() -> None:
    for title in [
        "Software Engineer",
        "Analyst",
        "Director",
        "Physician",
        "Data Scientist and Product Manager",
    ]:
        result = classify_role(title)
        if result.family is not RoleFamily.unknown:
            assert result.matched_evidence, title


def test_evidence_names_tier_and_rule_not_personal_data() -> None:
    # Evidence names the TIER + keyword/rule from the tables, never the input
    # verbatim in a way that could carry a value. Format: "tier<N>_<name>:
    # '<kw>' -> <family>".
    result = classify_role("Senior Software Engineer")
    assert result.matched_evidence.startswith("tier")
    assert "software engineer" in result.matched_evidence
    assert "->" in result.matched_evidence


def test_other_only_for_recognized_nonlisted_profession() -> None:
    result = classify_role("Physician")
    assert result.family is RoleFamily.other
    assert result.confidence is Confidence.high


def test_model_forbids_extra_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RoleClassification(family=RoleFamily.product, bogus="x")  # type: ignore[call-arg]


# --- Sprint 1.1 vocabulary-expansion decisions -------------------------------
# One dedicated assertion per documented edge case in docs/classification.md's
# decision table, so a future vocabulary change cannot silently flip one of
# these calls without a test failing.


def test_product_owner_stays_product_bare_po_stays_unmapped() -> None:
    # "PO" alone is deliberately NOT vocabulary (Purchase Order / too
    # ambiguous); only the unambiguous phrase "product owner" is mapped.
    assert classify_role("Product Owner").family is RoleFamily.product
    assert classify_role("PO").family is RoleFamily.unknown


@pytest.mark.parametrize("title", ["Scrum Master", "Agile Coach"])
def test_scrum_master_and_agile_coach_are_engineering_medium(title: str) -> None:
    # Delivery-facilitation roles, hedged to engineering at medium (documented
    # decision: overwhelmingly embedded in software delivery teams here).
    result = classify_role(title)
    assert result.family is RoleFamily.engineering_data_ai
    assert result.confidence is Confidence.medium


def test_cro_abbreviation_is_sales_medium_but_spelled_out_is_high() -> None:
    # "CRO" is ambiguous (Chief Revenue Officer vs Chief Risk Officer) -> the
    # revenue reading is chosen, but only at medium. Spelled out, the phrase
    # is unambiguous and gets high.
    abbrev = classify_role("CRO")
    assert abbrev.family is RoleFamily.sales_bd_partnerships
    assert abbrev.confidence is Confidence.medium
    spelled = classify_role("Chief Revenue Officer")
    assert spelled.family is RoleFamily.sales_bd_partnerships
    assert spelled.confidence is Confidence.high


def test_cpo_abbreviation_is_product_medium() -> None:
    # Ambiguous (Product vs People vs Privacy officer); product reading
    # chosen at medium given this app's tech/startup audience.
    result = classify_role("CPO")
    assert result.family is RoleFamily.product
    assert result.confidence is Confidence.medium


def test_cdo_abbreviation_is_engineering_medium_but_spelled_out_is_high() -> None:
    # Ambiguous (Chief Data Officer vs Chief Digital Officer); both readings
    # are technology-adjacent, so mapped at medium. Spelled out is high.
    abbrev = classify_role("CDO")
    assert abbrev.family is RoleFamily.engineering_data_ai
    assert abbrev.confidence is Confidence.medium
    spelled = classify_role("Chief Data Officer")
    assert spelled.family is RoleFamily.engineering_data_ai
    assert spelled.confidence is Confidence.high


def test_journalist_is_other_not_marketing() -> None:
    # Pure journalism is a distinct recognized profession (T3, other); content
    # and social-media roles stay in marketing_growth_content.
    result = classify_role("Jornalista")
    assert result.family is RoleFamily.other
    assert result.confidence is Confidence.high


def test_bare_cientista_is_left_unknown() -> None:
    # A bare "Cientista" (scientist, no domain) is genuinely ambiguous (data /
    # political / physical scientist, ...) -- deliberately unmapped rather
    # than forced.
    result = classify_role("Cientista")
    assert result.family is RoleFamily.unknown
    assert result.confidence is Confidence.unknown


def test_cientista_de_dados_is_still_engineering_high() -> None:
    # The bare "Cientista" exclusion above must not regress the qualified
    # "Cientista de Dados" (data scientist) phrase, which stays high.
    result = classify_role("Cientista de Dados")
    assert result.family is RoleFamily.engineering_data_ai
    assert result.confidence is Confidence.high


@pytest.mark.parametrize("title", ["Arquiteto", "Arquiteta"])
def test_bare_arquiteto_is_other_buildings_profession(title: str) -> None:
    # A context-free "Arquiteto(a)" reads as a building architect (T3,
    # other) -- only reached because none of T1's "arquiteto de
    # software/solucoes/dados/cloud" phrases matched.
    result = classify_role(title)
    assert result.family is RoleFamily.other
    assert result.confidence is Confidence.high


def test_arquiteto_de_software_stays_engineering_not_buildings() -> None:
    # The bare-"arquiteto" -> other rule above must never shadow the
    # qualified software/solutions/data/cloud architect phrases (T1 always
    # wins first).
    result = classify_role("Arquiteto de Software")
    assert result.family is RoleFamily.engineering_data_ai
    assert result.confidence is Confidence.high


def test_dono_da_padaria_is_founder_executive() -> None:
    # A genuine PT ownership claim ("Dono da Padaria" = owner of the bakery)
    # still fires the T0 ownership override.
    result = classify_role("Dono da Padaria")
    assert result.family is RoleFamily.founder_executive
    assert result.confidence is Confidence.high


@pytest.mark.parametrize(
    "title",
    ["Dono de Produto", "Dono do Produto", "Dona de Processo", "Dona do Servico"],
)
def test_dono_dona_false_friends_do_not_trigger_ownership(title: str) -> None:
    # PT mirror of the existing English "owner" false-friend guard: "Dono/
    # Dona de Produto/Processo/Serviço" is role stewardship (product/process/
    # service owner), not company ownership, so T0 must NOT fire.
    result = classify_role(title)
    assert result.family is not RoleFamily.founder_executive


def test_dono_de_produto_falls_through_to_product() -> None:
    result = classify_role("Dono de Produto")
    assert result.family is RoleFamily.product


@pytest.mark.parametrize("title", ["Empreendedor", "Empreendedora"])
def test_empreendedor_is_founder_executive_medium_not_high(title: str) -> None:
    # Weaker evidence than an explicit founder/owner claim (T0): a
    # self-identifier that does not always denote a formal ownership stake.
    result = classify_role(title)
    assert result.family is RoleFamily.founder_executive
    assert result.confidence is Confidence.medium


def test_qualidade_routes_to_operations_not_engineering() -> None:
    # Bare PT "Qualidade" overwhelmingly means manufacturing/process quality
    # in this context, not software QA (which is covered separately via
    # "qa"/"quality assurance"/"qa engineer").
    result = classify_role("Analista de Qualidade")
    assert result.family is RoleFamily.operations_people_finance_legal


def test_qa_engineer_still_engineering_despite_qualidade_decision() -> None:
    result = classify_role("QA Engineer")
    assert result.family is RoleFamily.engineering_data_ai
    assert result.confidence is Confidence.high


def test_bare_bi_abbreviation_is_not_expanded() -> None:
    # Bare "BI" stays unmapped (too ambiguous as a lone token), but the full
    # phrase "analista de bi" is unambiguous BI-analyst evidence (T1 phrase
    # rule) -- phrase-level context, not blind abbreviation expansion.
    assert classify_role("BI").family is RoleFamily.unknown
    result = classify_role("Analista de BI")
    assert result.family is RoleFamily.engineering_data_ai
    assert result.confidence is Confidence.high


def test_fiscal_is_left_unknown() -> None:
    # "Fiscal" (PT) is genuinely ambiguous between tax/finance ("Auditor
    # Fiscal") and a generic inspector role ("Fiscal de Trânsito") unrelated
    # to finance -- deliberately unmapped.
    result = classify_role("Fiscal")
    assert result.family is RoleFamily.unknown


def test_bare_especialista_is_left_unknown() -> None:
    # "Especialista" (specialist) alone names no domain -- too generic to map.
    result = classify_role("Especialista")
    assert result.family is RoleFamily.unknown


def test_enthusiasm_clause_is_not_functional_evidence() -> None:
    # Fable ruling (Sprint 1.1): "passionate about X" describes enthusiasm,
    # not employment -- the clause is discarded before matching. A bare
    # enthusiasm title is unknown; a real function ahead of the clause still
    # classifies normally. See docs/classification.md.
    assert classify_role("Apaixonado por Tecnologia").family is RoleFamily.unknown
    assert classify_role("Passionate about Technology").family is RoleFamily.unknown
    kept = classify_role("Engenheira de Software apaixonada por dados")
    assert kept.family is RoleFamily.engineering_data_ai
    assert kept.confidence is Confidence.high


def test_bare_analyst_and_coordinator_are_unknown_family() -> None:
    # Fable ruling (Sprint 1.1): bare analyst/analista is cross-domain and
    # bare coordenador/coordinator is a seniority word; neither may map to a
    # family. Seniority still buckets them independently.
    for title in ("Analyst", "Analista", "Coordenador", "Coordinator"):
        assert classify_role(title).family is RoleFamily.unknown


@pytest.mark.parametrize(
    "title",
    ["Resolvedor de Problemas", "Fazedor de Coisas", "Curioso Nato"],
)
def test_domain_empty_non_conventional_titles_are_unknown(title: str) -> None:
    result = classify_role(title)
    assert result.family is RoleFamily.unknown
    assert result.confidence is Confidence.unknown


def test_tech_lead_mixed_language_is_engineering() -> None:
    result = classify_role("Tech Lead na StartupX")
    assert result.family is RoleFamily.engineering_data_ai
    assert result.confidence is Confidence.high


def test_sdr_and_bdr_are_sales_high() -> None:
    assert classify_role("SDR").family is RoleFamily.sales_bd_partnerships
    assert classify_role("BDR").family is RoleFamily.sales_bd_partnerships


def test_founder_plus_cpo_ownership_still_dominates() -> None:
    # Ownership overrides even a NEW C-level acronym (not just CTO): T0 fires
    # before T1's "cpo" rule is ever consulted.
    result = classify_role("Founder & CPO")
    assert result.family is RoleFamily.founder_executive
    assert result.confidence is Confidence.high
