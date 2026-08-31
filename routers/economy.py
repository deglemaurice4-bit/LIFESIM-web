from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.economy_service import do_work, do_daily
from .auth import get_current_user  # non utilisé ici, on passe user_id en query

router = APIRouter()

class WorkResponse(BaseModel):
    success: bool
    data: dict

@router.post("/work/{user_id}")
async def api_work(user_id: int):
    try:
        result = await do_work(user_id)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/daily/{user_id}")
async def api_daily(user_id: int):
    try:
        result = await do_daily(user_id)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))