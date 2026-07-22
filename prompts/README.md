# prompts/

This folder will hold versioned prompt templates used for provider calls
(structured requests sent through the `ModelProvider` abstraction described
in `docs/architecture.md` and ADR 0002). No prompts exist yet in this
sprint: only `MockProvider` is wired end-to-end, and it does not call an
external model at all, so there is nothing to template. Prompt templates
will be added here as real provider integrations are implemented in later
sprints.
