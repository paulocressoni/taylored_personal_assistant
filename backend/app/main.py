"""Minimal FastAPI entrypoint for the containerized backend (M07)."""

from fastapi import FastAPI

app = FastAPI(title="Taylored Personal Assistant")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
