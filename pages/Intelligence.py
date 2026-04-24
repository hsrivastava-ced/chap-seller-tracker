"""Streamlit multipage entry for the Customer Intelligence view.

Thin wrapper so the real logic lives in intelligence_ui.py (importable
by tests) and so Streamlit auto-discovers it as the "Intelligence"
page in the navigation.
"""
from intelligence_ui import main

main()
