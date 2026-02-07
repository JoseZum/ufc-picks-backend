"""
Fight Card Image Controller - Generates downloadable fight card PNG
"""

from io import BytesIO

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.dependencies import Database, CurrentUser
from app.services.fight_card_image_service import generate_fight_card_png


router = APIRouter(tags=["fight-card"])


@router.get("/events/{event_id}/fight-card-image")
async def get_fight_card_image(
    event_id: int,
    db: Database,
    current_user: CurrentUser,
):
    """
    Generate and return a fight card PNG image for an event.

    The image shows all bouts in a 2-column poster layout with the
    authenticated user's picks highlighted (red/blue border).
    """
    # Fetch the event
    event_doc = await db["events"].find_one({"id": event_id})
    if not event_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found",
        )

    # Generate PNG
    png_bytes = await generate_fight_card_png(
        event=event_doc,
        db=db,
        user_id=current_user.id,
    )

    # Build filename
    event_name = event_doc.get("name", f"event-{event_id}").replace(" ", "-").lower()
    filename = f"fight-card-{event_name}.png"

    return StreamingResponse(
        BytesIO(png_bytes),
        media_type="image/png",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
