"""Shared helpers for the Streamlit dashboards (run with `uv run aecom dashboard`)."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import text

import aecom.handlers  # noqa: F401  (approvals publish events handled in-process)
from aecom.bootstrap import init
from aecom.db import get_engine

# Reference categorical palette (dataviz skill), fixed order — never cycled. Slots 1-3 are safe for any chart form.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
AGENT_ORDER = ["ceo", "researcher", "judge", "foreman", "finance", "store_ops", "marketing", "support", "supply"]


@st.cache_resource
def _boot() -> bool:
    init()
    return True


def sql(query: str, **params: object) -> pd.DataFrame:
    _boot()
    with get_engine().connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


def colors(n: int) -> list[str]:
    return SERIES[:n]
