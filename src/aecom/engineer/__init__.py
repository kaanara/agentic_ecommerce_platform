"""The Engineer agent (roadmap E3): writes adapters and webhooks on a branch and opens a PR.

The highest-risk agent, so containment lives in code, not only in the prompt (`agents/engineer/*`):

- it only ever works inside its own git clone, ``<ROOT>/data/engineer_workspace/`` (never the live checkout),
  created once with ``git clone --local`` of this repo, or ``git clone <ENGINEER_REPO_URL>`` when that is set;
- every path it touches is resolved and confined to that clone (symlinks and ``..`` rejected);
- writes are further limited to an explicit allowlist (store/integrations/channels adapters, the webhooks route,
  tests, one integrations doc); secrets, migrations, policy, deploy/CI config and this app's own safety-critical
  modules are always denied, even if a future allowlist entry would otherwise match;
- it works on exactly one branch ``engineer/<slug>`` at a time, never ``main``/``master``;
- opening a PR (``eng_open_pr``) is tier T3 in ``policy.yaml``, so the policy gate always parks it for a human;
  the tool body itself (run only once approved, see ``aecom.tools.remediation`` for the same pattern) additionally
  refuses an oversized diff, a denied path, anything that looks like a secret, or a HEAD that
  ``eng_run_tests`` has not already passed, and never pushes anything but that one branch;
- there is no tool that merges, deploys, restarts anything, edits the live checkout, or reads ``.env``.

Submodules:

- :mod:`aecom.engineer.workspace` - the clone itself, path containment, branch management and git/test plumbing.
- :mod:`aecom.engineer.github_api` - the diff-size cap, the secret scan and PR creation via ``aecom.net_guard``.
- :mod:`aecom.engineer.tools` - the ``eng_*`` tools, registered in ``aecom.tools.catalog.CATALOG`` by importing
  this module from the bottom of ``aecom.tools.catalog`` (mirroring how that module already registers the
  Sentinel's ``aecom.tools.diagnostics`` / ``aecom.tools.remediation`` tools from ``aecom.tools.__init__``).
"""

from __future__ import annotations
