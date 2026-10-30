# Entrypoint of the dashboard (`aecom dashboard`, the systemd unit): page config, login gate, navigation.
# The pages live in dashboard/app_pages/ and are registered in dashboard/_nav.py. Every page also calls
# require_login() itself (it is idempotent), so no page is ever reachable without the gate.
import streamlit as st

from _common import require_login
from _nav import build_navigation

st.set_page_config(page_title="aecom", page_icon=":material/hub:", layout="wide")
require_login()
build_navigation().run()
