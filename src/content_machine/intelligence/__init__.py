"""Intelligence Brief module (Gate A: models, loader, normalize, cluster,
ranking only). Fully offline: imports only stdlib and Pydantic (plus each
other). No network code, no provider imports, no LLM, no CLI command in this
gate, and nothing outside this package imports it.

See ``models.py`` for the three non-circular object families (SourceItem,
TopicCluster, RelevanceProfile), and ``ranking.py``'s module docstring for why
``RankedTopic`` is not constructed anywhere in Gate A code.
"""

from __future__ import annotations
