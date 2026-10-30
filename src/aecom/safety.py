"""R17: a shared guard against writing demo/simulated orders into a company database that holds REAL store sales.

``chaos.run_scenario`` already refuses to run against a non-disposable database (see
``aecom.chaos._is_disposable_db``); this module reuses that same definition so every helper that writes
test/demo data agrees on what counts as "safe to scribble on" - a company's production data must never be mixed
with fabricated orders, refunds and ledger lines.
"""

from __future__ import annotations

import os


class DemoDataRefused(RuntimeError):
    """Raised by ``assert_demo_data_allowed`` when demo/simulated writes would corrupt a real company database."""


def assert_demo_data_allowed(action: str) -> None:
    """Raise ``DemoDataRefused`` unless ``action`` may write demo/simulated data to the configured database.

    Allowed when any of:
    - ``AECOM_ALLOW_DEMO=1`` is set (an explicit, deliberate override), or
    - the configured database is disposable, i.e. a throwaway SQLite file under the system temp directory
      (see ``aecom.chaos._is_disposable_db``, reused here rather than re-implemented), or
    - the database holds no REAL store sales yet (``aecom.trust.real_sales()``) - a brand-new or demo-only
      company database, so ``aecom init --demo`` keeps working the first time it is run.

    Refused otherwise: writing simulated orders alongside real store sales would silently corrupt P&L, KPIs and
    the trust/autonomy evidence that real sales feed into.
    """
    if os.environ.get("AECOM_ALLOW_DEMO") == "1":
        return
    from aecom.chaos import _is_disposable_db
    from aecom.config import get_settings
    from aecom.trust import real_sales

    url = get_settings().database_url
    if _is_disposable_db(url):
        return
    if real_sales() == 0:
        return
    raise DemoDataRefused(
        f"refusing {action}: {url} already holds real store sales, so demo/simulated orders could not be told "
        "apart from real ones afterwards. Point DATABASE_URL at a disposable database (a file under the system "
        "temp dir), or set AECOM_ALLOW_DEMO=1 if you really mean it.")
