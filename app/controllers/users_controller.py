"""
Controlador de usuarios - Endpoints públicos para ver perfiles y picks de usuarios
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.core.dependencies import Database

router = APIRouter(prefix="/users", tags=["users"])


class UserProfileResponse(BaseModel):
    """Perfil público de un usuario."""
    id: str
    name: str
    avatar_url: Optional[str] = None
    created_at: datetime
    # Stats
    total_points: int = 0
    picks_total: int = 0
    picks_correct: int = 0
    perfect_picks: int = 0
    accuracy: float = 0.0


class UserPickResponse(BaseModel):
    """Pick de un usuario con información del bout."""
    id: str
    bout_id: int
    event_id: int
    event_name: Optional[str] = None
    event_date: Optional[str] = None
    picked_fighter_name: str
    picked_method: str
    picked_round: Optional[int] = None
    is_correct: Optional[bool] = None
    points_awarded: int = 0
    locked: bool = False
    created_at: datetime
    # Bout info
    fighter_red: Optional[str] = None
    fighter_blue: Optional[str] = None
    weight_class: Optional[str] = None
    result: Optional[dict] = None


@router.get("/{user_id}", response_model=UserProfileResponse)
async def get_user_profile(
    user_id: str,
    db: Database
):
    """
    Obtener el perfil público de un usuario.
    """
    user = await db["users"].find_one({"_id": user_id})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )

    return UserProfileResponse(
        id=user.get("_id"),
        name=user.get("name", "Unknown"),
        avatar_url=user.get("profile_picture"),
        created_at=user.get("created_at", datetime.utcnow()),
        total_points=user.get("total_points", 0),
        picks_total=user.get("picks_total", 0),
        picks_correct=user.get("picks_correct", 0),
        perfect_picks=user.get("perfect_picks", 0),
        accuracy=user.get("accuracy", 0.0)
    )


@router.get("/{user_id}/picks", response_model=list[UserPickResponse])
async def get_user_picks(
    user_id: str,
    db: Database,
    event_id: Optional[int] = Query(None, description="Filter by event ID"),
    year: Optional[int] = Query(None, description="Filter by year"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: correct, incorrect, pending"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of picks to return"),
    skip: int = Query(0, ge=0, description="Number of picks to skip")
):
    """
    Obtener los picks de un usuario con filtros.

    Los picks se pueden filtrar por:
    - event_id: ID de un evento específico
    - year: Año del evento
    - status: correct, incorrect, pending

    Solo muestra picks de peleas que ya tienen resultado público.
    """
    # Verificar que el usuario existe
    user = await db["users"].find_one({"_id": user_id})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )

    resulted_bout_ids = set(await db["bouts"].distinct(
        "id",
        {
            "result": {
                "$exists": True,
                "$nin": [None, {}],
            }
        },
    ))
    resulted_bout_ids.update(
        await db["bout_details"].distinct(
            "bout_id",
            {
                "result": {
                    "$exists": True,
                    "$nin": [None, {}],
                }
            },
        )
    )
    query = {
        "user_id": user_id,
        "bout_id": {"$in": list(resulted_bout_ids)},
    }

    if event_id:
        query["event_id"] = event_id

    if status_filter:
        if status_filter == "correct":
            query["is_correct"] = True
        elif status_filter == "incorrect":
            query["is_correct"] = False
        elif status_filter == "pending":
            query["is_correct"] = None

    # Obtener picks
    cursor = db["picks"].find(query).sort("created_at", -1).skip(skip).limit(limit)
    picks = await cursor.to_list(length=limit)

    if not picks:
        return []

    # Obtener información de eventos y bouts
    event_ids = list(set(p["event_id"] for p in picks))
    bout_ids = list(set(p["bout_id"] for p in picks))

    # Fetch events
    events_cursor = db["events"].find({"id": {"$in": event_ids}})
    events = await events_cursor.to_list(length=None)
    events_map = {e["id"]: e for e in events}

    # Filtrar por año si se especificó
    if year:
        picks = [
            p for p in picks
            if str(
                events_map.get(p["event_id"], {}).get("date")
                or events_map.get(p["event_id"], {}).get("event_date")
                or ""
            ).startswith(str(year))
        ]

    # Fetch bouts
    bouts_cursor = db["bouts"].find({"id": {"$in": bout_ids}})
    bouts = await bouts_cursor.to_list(length=None)
    bouts_map = {b["id"]: b for b in bouts}
    details_cursor = db["bout_details"].find(
        {"bout_id": {"$in": bout_ids}, "result": {"$nin": [None, {}]}}
    )
    details = await details_cursor.to_list(length=None)
    detail_results = {
        detail["bout_id"]: detail["result"]
        for detail in details
        if detail.get("result")
    }

    # Construir respuesta
    result = []
    for p in picks:
        event = events_map.get(p["event_id"], {})
        bout = bouts_map.get(p["bout_id"], {})
        fighters = bout.get("fighters", {})

        result.append(UserPickResponse(
            id=p.get("_id", p.get("id", "")),
            bout_id=p.get("bout_id"),
            event_id=p.get("event_id"),
            event_name=event.get("name"),
            event_date=str(event.get("date") or event.get("event_date") or "") or None,
            picked_fighter_name=p.get("picked_fighter_name", ""),
            picked_method=p.get("picked_method"),
            picked_round=p.get("picked_round"),
            is_correct=p.get("is_correct"),
            points_awarded=p.get("points_awarded", 0),
            locked=True,
            created_at=p.get("created_at", datetime.utcnow()),
            fighter_red=fighters.get("red", {}).get("fighter_name"),
            fighter_blue=fighters.get("blue", {}).get("fighter_name"),
            weight_class=bout.get("weight_class"),
            result=bout.get("result") or detail_results.get(p.get("bout_id"))
        ))

    return result


@router.get("/{user_id}/picks/stats", response_model=dict)
async def get_user_picks_stats(
    user_id: str,
    db: Database,
    year: Optional[int] = Query(None, description="Filter by year")
):
    """
    Obtener estadísticas de picks de un usuario.

    Retorna estadísticas como:
    - Total de picks
    - Picks correctos/incorrectos
    - Accuracy
    - Picks por método (DEC, KO/TKO, SUB)
    - Picks por evento
    """
    # Verificar que el usuario existe
    user = await db["users"].find_one({"_id": user_id})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )

    # Public stats include only bouts whose result has been published.
    match_stage = {"user_id": user_id}

    # Pipeline de agregación
    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "events",
                "localField": "event_id",
                "foreignField": "id",
                "as": "event"
            }
        },
        {"$unwind": {"path": "$event", "preserveNullAndEmptyArrays": True}},
        {
            "$lookup": {
                "from": "bouts",
                "localField": "bout_id",
                "foreignField": "id",
                "as": "bout",
            }
        },
        {"$unwind": "$bout"},
        {
            "$lookup": {
                "from": "bout_details",
                "localField": "bout_id",
                "foreignField": "bout_id",
                "as": "bout_detail",
            }
        },
        {
            "$unwind": {
                "path": "$bout_detail",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$match": {
                "$or": [
                    {
                        "bout.result": {
                            "$exists": True,
                            "$nin": [None, {}],
                        }
                    },
                    {
                        "bout_detail.result": {
                            "$exists": True,
                            "$nin": [None, {}],
                        }
                    },
                ]
            }
        },
    ]

    # Filtrar por año si se especificó
    if year:
        year_start = datetime(year, 1, 1)
        year_end = datetime(year + 1, 1, 1)
        pipeline.append({
            "$match": {
                "$or": [
                    {
                        "event.date": {
                            "$gte": year_start,
                            "$lt": year_end,
                        }
                    },
                    {"event.event_date": {"$regex": f"^{year}"}},
                ]
            }
        })

    # Agregar estadísticas
    pipeline.extend([
        {
            "$group": {
                "_id": None,
                "total_picks": {"$sum": 1},
                "correct_picks": {
                    "$sum": {"$cond": [{"$eq": ["$is_correct", True]}, 1, 0]}
                },
                "incorrect_picks": {
                    "$sum": {"$cond": [{"$eq": ["$is_correct", False]}, 1, 0]}
                },
                "pending_picks": {
                    "$sum": {"$cond": [{"$eq": ["$is_correct", None]}, 1, 0]}
                },
                "total_points": {"$sum": "$points_awarded"},
                "perfect_picks": {
                    "$sum": {"$cond": [{"$eq": ["$points_awarded", 3]}, 1, 0]}
                },
                "dec_picks": {
                    "$sum": {"$cond": [{"$eq": ["$picked_method", "DEC"]}, 1, 0]}
                },
                "ko_picks": {
                    "$sum": {"$cond": [{"$eq": ["$picked_method", "KO/TKO"]}, 1, 0]}
                },
                "sub_picks": {
                    "$sum": {"$cond": [{"$eq": ["$picked_method", "SUB"]}, 1, 0]}
                }
            }
        }
    ])

    # PyMongo Async returns a coroutine here; Motor returned the cursor
    # directly. Without the await this raised on the next line.
    cursor = await db["picks"].aggregate(pipeline)
    results = await cursor.to_list(length=1)

    if not results:
        return {
            "total_picks": 0,
            "correct_picks": 0,
            "incorrect_picks": 0,
            "pending_picks": 0,
            "total_points": 0,
            "perfect_picks": 0,
            "accuracy": 0.0,
            "by_method": {"DEC": 0, "KO/TKO": 0, "SUB": 0}
        }

    stats = results[0]
    evaluated_picks = stats["correct_picks"] + stats["incorrect_picks"]
    accuracy = (stats["correct_picks"] / evaluated_picks * 100) if evaluated_picks > 0 else 0.0

    return {
        "total_picks": stats["total_picks"],
        "correct_picks": stats["correct_picks"],
        "incorrect_picks": stats["incorrect_picks"],
        "pending_picks": stats["pending_picks"],
        "total_points": stats["total_points"],
        "perfect_picks": stats["perfect_picks"],
        "accuracy": round(accuracy, 1),
        "by_method": {
            "DEC": stats["dec_picks"],
            "KO/TKO": stats["ko_picks"],
            "SUB": stats["sub_picks"]
        }
    }
