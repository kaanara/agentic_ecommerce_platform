"""Agent tools. ``catalog`` holds the built-ins and the ``@tool`` registry (CATALOG); importing this package also
registers the Sentinel's read-only diagnostics and its gated remediation tools, so CATALOG is complete whenever
``aecom.tools.catalog`` is imported.

``diagnostics`` and ``remediation`` import ``catalog`` (which never imports this package's other modules), so there is no cycle.
"""

from aecom.tools import (
    diagnostics,  # noqa: F401  (registers diagnostic tools in CATALOG)
    remediation,  # noqa: F401  (registers the Sentinel's remediation tools in CATALOG)
)
