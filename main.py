import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from routes import auth, player, economy
from websocket.manager import manager
from scheduler.tasks import start_scheduler

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Démarrer le scheduler (le même que celui du bot)
    asyncio.create_task(start_scheduler())
    yield
    # Nettoyage...

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre en prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(player.router, prefix="/api/player", tags=["player"])
app.include_router(economy.router, prefix="/api/economy", tags=["economy"])

# WebSocket
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_json()
            await manager.handle_message(user_id, data)
    except WebSocketDisconnect:
        manager.disconnect(user_id)