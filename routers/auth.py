from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from database import get_user
import secrets

router = APIRouter()

class LoginRequest(BaseModel):
    telegram_id: int
    username: str = ""
    full_name: str = ""

# Stockage temporaire (à remplacer par Redis)
sessions = {}

@router.post("/login")
async def login(req: LoginRequest):
    user = await get_user(req.telegram_id, req.username, req.full_name)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    token = secrets.token_urlsafe(32)
    sessions[token] = user["user_id"]
    return {
        "token": token,
        "user_id": user["user_id"],
        "full_name": user.get("full_name", "Joueur"),
        "balance": user.get("balance", 0),
        "level": user.get("level", 1),
        "xp": user.get("xp", 0),
        "health": user.get("health", 100),
        "energy": user.get("energy", 100),
        "hunger": user.get("hunger", 100),
        "happiness": user.get("happiness", 100),
        "stress": user.get("stress", 0),
        "karma": user.get("karma", 0),
        "prestige": user.get("prestige", 0),
    }

async def get_current_user(token: str = Depends(lambda: None)) -> int:
    # À utiliser avec un header Authorization: Bearer <token>
    # Pour simplifier, on passe le user_id en query pour le moment
    # Dans une vraie API, on extrait le token du header
    pass