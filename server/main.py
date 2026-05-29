from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

from server.database import init_db
from server.routers import zones, maintenance, thanks, push

app = FastAPI(title="arigato")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(zones.router)
app.include_router(maintenance.router)
app.include_router(thanks.router)
app.include_router(push.router)

PWA_DIR = Path(__file__).parent.parent / "pwa"
app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")

@app.on_event("startup")
def startup():
    init_db()
