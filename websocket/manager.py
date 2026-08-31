from typing import Dict
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)

    async def send_personal(self, user_id: int, data: dict):
        ws = self.active_connections.get(user_id)
        if ws:
            await ws.send_json(data)

    async def broadcast(self, data: dict):
        for ws in self.active_connections.values():
            await ws.send_json(data)

    async def handle_message(self, user_id: int, data: dict):
        # À remplir avec les actions temps réel (défis, casino, etc.)
        pass

manager = ConnectionManager()