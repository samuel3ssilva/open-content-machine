# Examples

Synthetic fixtures used by the test suite and the CLI demo. Nothing here is
real data -- see the top-level privacy rules in `CLAUDE.md` and
`docs/privacy.md`.

## Intelligence Brief fixtures

- `intelligence-signals-synthetic.json` -- 41 synthetic `SourceItem` records
  exercised by `tests/test_intelligence_*.py`.
- `intelligence-signals-invalid.json` -- deliberately malformed items, one
  per failure mode the loader must handle without crashing (unknown field,
  missing required field, invalid date, unknown topic tag, invalid enum
  literal).
- `intelligence-profile-synthetic.json` -- a synthetic `RelevanceProfile`
  (the real Founder profile is private and never enters this repo).

### Authoring guidance: downstream release notes

A non-subject `release_note` can genuinely be a PRIMARY ARTIFACT of the
*publishing* project, not mere secondary news about someone else -- e.g. a
downstream project's own release note stating "upgraded to VendorA 3.0,
requires migration" is that downstream project's first-party artifact, even
though it is also coverage of VendorA. To be scored as such (rather than
Founder decision D2's `secondary_news_uncorroborated`), **the downstream
project must be listed in that item's own `subject_entity_ids`** --
`publisher_id` alone is not enough, since evidence polarity is judged per
(item, cluster-subject) pair. See the `contains_benefit_or_performance_claim`
docstring on `SourceItem` (`src/content_machine/intelligence/models.py`) for
the parallel authoring rule for benefit/performance claims.
