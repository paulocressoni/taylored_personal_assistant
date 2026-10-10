"""Application version — single source of truth.

Every runtime consumer of the version imports ``__version__`` from here
instead of hardcoding it:
  - FastAPI metadata (``app.main``)
  - the /health endpoint (``app.api.routes``)
  - the frontend footer (fetched from /health at runtime)

Release automation (release-please) keeps this in sync with
``backend/pyproject.toml`` (``[project].version``), ``frontend/package.json``
and the ``v<version>`` git tag — no manual edits required at release time.

Why a plain module and not ``importlib.metadata``?
  The Docker image does NOT pip-install the package — it only copies
  ``app/`` and runs uvicorn — so ``importlib.metadata.version("backend")``
  would raise inside the container. A module always works everywhere.
"""

# The trailing marker is read by release-please's "generic" updater: it rewrites
# the version value on THIS line only (see release-please-config.json extra-files).
__version__ = "0.4.0"  # x-release-please-version
