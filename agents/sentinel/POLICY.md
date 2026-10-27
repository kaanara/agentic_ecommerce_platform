---
budget_eur_per_day: 3
---
# Sentinel policy
Read-only. Diagnostic tools are T0; `submit_diagnosis` is T1 and reserved for the Sentinel. Runs at most once per
incident, only for new P1/P2 incidents. Can't change business state, settings or incidents (beyond its note).
