"""Static AST scan enforcing the "only config/ reads the environment" rule
(Gate E0, R8).

``docs/architecture.md`` §2 and ``src/content_machine/config/settings.py``'s
own module docstring both state that ``content_machine.config`` is the ONLY
module allowed to read environment variables -- every other module receives
a ``Settings`` instance or explicit arguments instead. Before this test,
nothing in the repository actually enforced that rule: a future module could
add an ``os.getenv(...)`` call and nothing would catch it.

Mirrors the existing static scans in ``tests/test_connectors_no_network.py``
(full-tree ``ast.walk``, not just ``tree.body``, so a nested/conditional read
is still caught) and their well-documented, honestly-stated limitation: this
scan cannot see (a) TRANSITIVE reads -- a clean module that calls into a
dirty one two hops away is not caught by scanning that one file in
isolation, though scanning every module in the tree individually
substantially closes that gap here; (b) dynamic access via
``getattr(os, "environ")``/``getattr(os, "getenv")`` or similar, which no
static AST walk can resolve. This is a cheap, fast, additional check, not an
exhaustive guarantee -- exactly the same posture as the no-network scan.

**R8 alias-tracking improvement (Part 2a correction, Finding 3).** The scan
now also tracks ``import os as <alias>`` and flags ``<alias>.environ``/
``<alias>.getenv``/``<alias>.environb`` -- a purely static rename
(``import os as o; o.getenv(...)``) is fully resolvable by an AST walk
(unlike the genuinely dynamic gaps below) and a prior revision of this scan
missed it. The following residual gaps remain honestly disclosed, NOT
closed by the alias tracking above, and this scan does not claim to be
exhaustive:

* ``getattr(os, "environ")``/``getattr(os, "getenv")`` (or similar
  string-computed attribute access) -- no static AST walk can resolve a
  dynamically-computed attribute name.
* ``posix.environ`` -- on POSIX systems ``os.environ`` is itself backed by
  the ``posix`` module; importing ``posix`` directly and reading
  ``posix.environ`` bypasses ``os`` entirely and this scan does not track
  ``posix`` as an alias of ``os``.
* Indirect module access, e.g. ``importlib.import_module("os")`` bound to
  an arbitrary local name, or ``os.__dict__["environ"]`` -- neither is a
  literal ``os``/tracked-alias attribute access this scan's ``ast.Attribute``
  matching can see.
* Subprocess environment INHERITANCE (a child process started via
  ``subprocess`` inheriting the parent's environment) -- a different
  concern from this module reading an environment variable's VALUE into
  Python code, and out of scope for what this scan checks.
* TRANSITIVE reads, as already stated above.

Detects, at minimum (per the ticket's required minimum):

* ``os.environ`` (as a bare attribute access, subscripted, or via ``.get``)
* ``os.getenv(...)``
* ``os.environb`` (the bytes-keyed twin of ``os.environ``)
* ``from os import environ`` / ``from os import getenv``
* ``import os as <alias>`` followed by ``<alias>.environ``/
  ``<alias>.getenv``/``<alias>.environb``

Allowlists ``content_machine/config/`` only.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = REPO_ROOT / "src" / "content_machine"
_ALLOWED_ENV_READING_DIR = _PACKAGE_ROOT / "config"

_FORBIDDEN_OS_ATTRS = {"environ", "getenv", "environb"}


def _all_python_files_outside_config() -> list[Path]:
    return sorted(
        path
        for path in _PACKAGE_ROOT.rglob("*.py")
        if _ALLOWED_ENV_READING_DIR not in path.parents
    )


def _env_reads_in_module(path: Path) -> list[str]:
    """Return a list of human-readable violation descriptions found in
    ``path``, or an empty list if the module reads no environment variable.

    Same TYPE_CHECKING carve-out as the existing no-network scan (a
    type-only import guarded by ``if TYPE_CHECKING:`` never executes at
    runtime, so it cannot ever read the environment) -- and the same
    documented, conservative limitation: ``ast.walk`` still enumerates a
    TYPE_CHECKING block's children independently of the outer node being
    skipped by this loop's own ``continue``, so this carve-out only ever
    matters for the (empty, in practice) case of an `os.getenv` call that
    appears ONLY inside such a block and nowhere else.

    **R8 alias tracking (Part 2a correction, Finding 3).** A first pass
    collects every name a plain ``import os as <alias>`` binds in this
    module (``"os"`` itself is always included, whether or not it is ever
    imported under that name); the second pass then treats an attribute
    access on ANY of those names as equivalent to ``os.<attr>`` -- this
    resolves the purely static ``import os as o; o.getenv(...)`` rename,
    which (unlike ``getattr``-based or ``posix``-based access -- see the
    module docstring's disclosed residual gaps) an AST walk can fully see.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    os_aliases = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os" and alias.asname:
                    os_aliases.add(alias.asname)

    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Attribute)
            and node.test.attr == "TYPE_CHECKING"
        ):
            continue

        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in _FORBIDDEN_OS_ATTRS:
                    violations.append(f"from os import {alias.name}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _FORBIDDEN_OS_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        ):
            # Catches os.environ (bare, subscripted via the enclosing
            # Subscript node, and os.environ.get(...) via the enclosing
            # Call node), os.getenv(...), os.environb, and the same three
            # via any `import os as <alias>` rename -- ast.walk visits this
            # Attribute node regardless of what wraps it.
            violations.append(f"{node.value.id}.{node.attr}")

    return violations


def test_no_environment_variable_read_outside_config_package() -> None:
    files = _all_python_files_outside_config()
    assert len(files) >= 30  # sanity: the scan is actually walking a real tree
    offenders: dict[str, list[str]] = {}
    for path in files:
        found = _env_reads_in_module(path)
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = found
    assert not offenders, f"environment read(s) outside config/: {offenders}"


def test_config_package_itself_is_excluded_from_the_scan() -> None:
    """Sanity check on the scan's own filtering: settings.py (which DOES
    legitimately read the environment via pydantic-settings' env_prefix
    machinery) must not appear in the scanned file list."""
    files = _all_python_files_outside_config()
    assert not any(_ALLOWED_ENV_READING_DIR in path.parents for path in files)
    assert (_ALLOWED_ENV_READING_DIR / "settings.py").exists()


def test_scan_walks_the_full_tree_not_just_module_body() -> None:
    """Regression guard for the scan itself, mirroring
    test_connectors_no_network.py's identical guard: an os.getenv call
    nested inside a function (not at module top-level / tree.body) must
    still be caught -- proves the scan uses ast.walk(tree), not tree.body."""
    nested_source = 'def f():\n    import os\n    return os.getenv("X")\n'
    tree = ast.parse(nested_source)
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _FORBIDDEN_OS_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            found = True
    assert found, "ast.walk must see an os.getenv(...) call nested inside a function body"


def test_scan_catches_os_environ_get() -> None:
    source = 'import os\nvalue = os.environ.get("X")\n'
    tree = ast.parse(source)
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in _FORBIDDEN_OS_ATTRS
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ]
    assert found, "os.environ.get(...) must be caught"


def test_scan_catches_os_environ_subscript() -> None:
    source = 'import os\nvalue = os.environ["X"]\n'
    tree = ast.parse(source)
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in _FORBIDDEN_OS_ATTRS
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ]
    assert found, "os.environ[...] must be caught"


def test_scan_catches_from_os_import_environ_and_getenv() -> None:
    source = "from os import environ, getenv\n"
    tree = ast.parse(source)
    caught = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "os"
        for alias in node.names
        if alias.name in _FORBIDDEN_OS_ATTRS
    }
    assert caught == {"environ", "getenv"}


def test_scan_catches_os_environb() -> None:
    source = 'import os\nvalue = os.environb.get(b"X")\n'
    tree = ast.parse(source)
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in _FORBIDDEN_OS_ATTRS
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ]
    assert found, "os.environb must be caught"


def test_scan_catches_import_os_as_alias_getenv(tmp_path: Path) -> None:
    """R8 alias-tracking improvement (Part 2a correction, Finding 3): the
    purely static form ``import os as o; o.getenv(...)`` must be caught --
    unlike the genuinely dynamic gaps disclosed in the module docstring
    (``getattr(os, ...)``, ``posix.environ``, ``importlib``-computed module
    references, ``os.__dict__`` access), an import-alias rename is fully
    resolvable by an AST walk, and a prior revision of this scan missed it.

    Exercises the ACTUAL checker function (``_env_reads_in_module``) against
    a real file on disk, not hand-rolled ``ast.walk`` matching against a
    parsed-in-memory tree as the ``test_scan_catches_os_*`` tests above do --
    those tests prove the underlying Attribute-node shape is right; this one
    proves the alias-tracking fix in ``_env_reads_in_module`` itself works
    end to end."""
    module_path = tmp_path / "aliased_env_read.py"
    module_path.write_text('import os as o\nvalue = o.getenv("X")\n', encoding="utf-8")
    assert _env_reads_in_module(module_path) == ["o.getenv"]


def test_scan_catches_import_os_as_alias_environ_and_environb(tmp_path: Path) -> None:
    """The same alias tracking must also cover ``environ`` and ``environb``,
    not just ``getenv`` -- all three forbidden attrs are equally reachable
    through a rename."""
    module_path = tmp_path / "aliased_env_read_full.py"
    module_path.write_text(
        "import os as opsys\n"
        'a = opsys.environ.get("X")\n'
        'b = opsys.environb.get(b"X")\n',
        encoding="utf-8",
    )
    violations = _env_reads_in_module(module_path)
    assert set(violations) == {"opsys.environ", "opsys.environb"}
