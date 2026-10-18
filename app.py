# Vercel FastAPI entrypoint. Locally: uvicorn blastgate.api:app --port 8000
from blastgate.api import app  # noqa: F401
