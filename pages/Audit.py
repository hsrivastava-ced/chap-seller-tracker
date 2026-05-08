"""Streamlit multipage entry for the Audit / Activity page.

Thin wrapper so the real logic lives in audit_ui.py (importable by
tests) and Streamlit auto-discovers it as the "Audit" page in the
sidebar nav. Gated to super_admin only — the gate is inside
audit_ui.main().
"""
from audit_ui import main

main()
