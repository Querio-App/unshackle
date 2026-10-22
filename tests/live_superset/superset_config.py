# Test-only Superset config (mounted by docker-compose.yml). Not for production.
SECRET_KEY = "unshackle-live-test-only-secret-key"
FAB_ADD_SECURITY_API = True  # /api/v1/security/roles/
TALISMAN_ENABLED = False
