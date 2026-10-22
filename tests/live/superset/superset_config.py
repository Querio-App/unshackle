# Test-only Superset config (mounted by docker-compose.yml). Not for production.
import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]  # generated per run by tests/live/harness.py
FAB_ADD_SECURITY_API = True  # /api/v1/security/roles/
TALISMAN_ENABLED = False
