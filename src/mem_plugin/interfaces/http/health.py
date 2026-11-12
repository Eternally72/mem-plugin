"""Process liveness probe; does not claim to continuously validate database availability."""

from starlette.requests import Request
from starlette.responses import JSONResponse


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
