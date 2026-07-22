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
    # A bare generic token with no domain -> weak evidence.
    result = classify_role("Analyst")
    assert result.confidence is Confidence.low
    assert result.family is not RoleFamily.unknown
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
