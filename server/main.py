from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path

from server.database import init_db
from server.routers import zones, maintenance, thanks, push, users, admin, timelapse, presence
# spirit（地霊の脳）は読み込み失敗でも本体を巻き込まない（切り分け用の保険）
try:
    from server.routers import spirit
except Exception as _e:  # noqa: BLE001
    print(f"[warn] spirit router load failed: {_e}")
    spirit = None

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
app.include_router(users.router)
app.include_router(admin.router)
app.include_router(timelapse.router)
app.include_router(presence.router)
if spirit is not None:
    app.include_router(spirit.router)

PWA_DIR = Path(__file__).parent.parent / "pwa"

@app.get("/zone/{zone_id}")
async def serve_zone(zone_id: str):
    return FileResponse(PWA_DIR / "index.html")

@app.get("/admin")
async def serve_admin():
    return FileResponse(PWA_DIR / "admin.html")

app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")

@app.on_event("startup")
def startup():
    init_db()
