"""Tests for deterministic role-family classification."""

from __future__ import annotations

import pytest

from content_machine.audience.classify import (
    Confidence,
    RoleClassification,
    RoleFamily,
    classify_role,
)

# At least three positive, unambiguous (HIGH) cases per listed family.
_FAMILY_POSITIVE_CASES: dict[RoleFamily, list[str]] = {
    RoleFamily.founder_executive: ["Founder", "CFO", "Chief Executive Officer"],
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


def test_generic_leadership_token_is_medium() -> None:
    result = classify_role("Director")
    assert result.confidence is Confidence.medium
    assert result.family is RoleFamily.founder_executive


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


def test_evidence_is_rule_not_personal_data() -> None:
    # Evidence names a keyword/rule from the tables, never the input verbatim in
    # a way that could carry a value -- here the input has no personal data, but
    # the format is asserted: "keyword '<kw>' -> <family>".
    result = classify_role("Senior Software Engineer")
    assert result.matched_evidence.startswith("keyword '")
    assert "->" in result.matched_evidence


def test_other_only_for_recognized_nonlisted_profession() -> None:
    result = classify_role("Physician")
    assert result.family is RoleFamily.other
    assert result.confidence is Confidence.high


def test_model_forbids_extra_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RoleClassification(family=RoleFamily.product, bogus="x")  # type: ignore[call-arg]
