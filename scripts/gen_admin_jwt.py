#!/usr/bin/env python3
"""Generate admin JWT for Sycode strategy cache refresh."""
import jwt, time, sys, os
from pathlib import Path

# JWT signing secret — MUST match the Sycode server's JWT_SECRET (server/.env).
# Sourced from env or the existing secret store; never hardcoded. Mirrors how
# the running server authenticates admin JWTs.
_CRED_ENV_FILE = Path(os.environ.get("SYCODE_CREDENTIAL_ENV_FILE", "/home/frank/.hermes/secrets/sycode-credential.env"))
_SYC_SERVER_ENV = Path(os.environ.get("SYCODE_SERVER_ENV", "/home/frank/sycode-trading/server/.env"))
for _f in (_CRED_ENV_FILE, _SYC_SERVER_ENV):
    if _f.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(_f, override=False)
        except Exception:
            pass

SECRET = os.environ.get("JWT_SECRET")
if not SECRET:
    print("[FATAL] JWT_SECRET not set. Set JWT_SECRET or ensure "
          f"{_SYC_SERVER_ENV} defines it (server/.env).", file=sys.stderr)
    sys.exit(3)
now = int(time.time())

payload = {
    "userId": "jarvis-admin",
    "email": "jarvis-admin@sycode.local",
    "role": "admin",
    "iat": now,
    "exp": now + 604800  # 7 days
}

token = jwt.encode(payload, SECRET, algorithm="HS256")
if "--token-only" in sys.argv:
    print(token)
else:
    print(f"Admin JWT (expires in 7d):")
    print(token)
    print(f"\nTo refresh strategy cache:")
    print(f"curl -s -X POST http://localhost:3001/api/strategies/cache/refresh \\")
    print(f"  -H 'Authorization: Bearer {token}' \\")
    print(f"  -H 'Content-Type: application/json'")
