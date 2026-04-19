from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"ok": True, "version": "0.0.1"}
