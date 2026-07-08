import uuid

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()


@router.get("/images/{image_id}")
async def get_image(request: Request, image_id: str):
    try:
        iid = uuid.UUID(image_id)
    except ValueError:
        raise HTTPException(404, "not found")
    row = await request.app.state.pool.fetchrow(
        "select mime, bytes from images where id=$1", iid)
    if row is None:
        raise HTTPException(404, "not found")
    return Response(content=row["bytes"], media_type=row["mime"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})
