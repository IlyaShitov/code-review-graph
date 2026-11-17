# Django enrichment

The resolver from `django-graph-enricher` is bundled in
`code_review_graph/django_enricher/`; its original tests are in
`tests/django_enricher/`.

Full builds and incremental updates run it automatically when `manage.py`
exists at the repository root or up to two directories below it. This includes
MCP, CLI and watcher builds. An update that changes nothing in the graph skips
enrichment when the graph already has it; a graph built without it (by the
upstream version) is enriched by such an update too. Library code in `.venv` is
not tracked, so after upgrading a library run a full rebuild to re-resolve
calls through it.
No external hook, Docker container, Django installation or additional dependency
is needed. Project code is parsed statically and never imported or executed.

The resolver adds queryset/manager chains, inherited method calls, permission
filter calls, module constants and class labels. It reads Python files already
indexed by the graph, excluding migrations. Referenced library source is read
from `.venv` or `venv` when available.

Enrichment uses the build's SQLite connection and write lock, including custom
database locations. Its changes are atomic; a write failure aborts the build
instead of silently reporting an enriched graph. The result contains
`django_enrichment` with resolution/injection counts and unreadable source files.
For non-Django repositories, and when enrichment was skipped, the value is `null`.

Edges the resolver adds or rebinds carry a `rule` (`queryset_chain`, `mro`,
`permission_filter`, `constant_ref`, `celery_task`, `celery_beat`). Query
responses include it in each edge, and the compact format shows it in place of
the confidence tier.

Normal postprocessing runs afterwards, so FTS sees the added constants and
labels. With full postprocessing, flows and communities are recomputed across
the graph because enrichment can change links outside the re-parsed files.
Existing constants keep their IDs and community assignments.
`postprocess="none"` still skips FTS and other derived indexes; run
postprocessing separately when needed.

FTS accepts Unicode queries, including Russian labels such as `План рейса`
and mixed queries such as `Organization "Тип организации"`. A complete match
of `Meta.verbose_name` or `Meta.verbose_name_plural` ranks ahead of field-label
mentions, including with `limit=1`; exact code identifiers retain first priority.
Label matching ignores case and repeated whitespace. `е` and `ё` remain distinct.
These search changes work with an existing enriched FTS index; rebuilding it
is not required, but a running MCP server must reload the updated code.

Static inference retains the original resolver's limits: it does not model
control flow, ambiguous variable types or arbitrary dynamic calls. Only the
nearest discovered `manage.py` determines the source root.
