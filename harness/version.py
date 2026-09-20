"""Single source of truth for the harness release version.

Kept import-free so any module (server, isolation backends, deploy tooling)
can read it without triggering the package's heavier imports. pyproject.toml
reads it via `[tool.setuptools.dynamic]`.
"""

__version__ = "0.2.114"
