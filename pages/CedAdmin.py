"""Streamlit multipage entry for the CedCommerce admin dashboard.

Thin wrapper so the real logic lives in cedadmin_ui.py (importable
by tests) and Streamlit auto-discovers it as the "CedAdmin" page in
the navigation.
"""
from cedadmin_ui import main

main()
