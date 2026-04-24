"""Multipage entry for the admin UI.

Streamlit auto-discovers `pages/*.py` when `dashboard.py` is the main
script. Keeping `admin_ui.py` at the repo root lets it stay importable
(and runnable standalone via `streamlit run admin_ui.py`); this wrapper
just delegates so the dashboard sidebar's `st.page_link("pages/Admin.py",
...)` resolves.
"""
from admin_ui import main

main()
