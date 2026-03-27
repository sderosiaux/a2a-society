from __future__ import annotations

from fastapi import HTTPException, Request


def require_auth(token: str | None):
    """Returns a FastAPI dependency that validates bearer tokens."""
    if token is None:
        # No auth configured -- allow all (local dev mode)
        async def no_auth():
            pass

        return no_auth

    async def check_auth(request: Request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        if auth_header[7:] != token:
            raise HTTPException(status_code=403, detail="Invalid token")

    return check_auth
