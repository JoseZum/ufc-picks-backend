"""
🔌 Database Connection Setup - MongoDB Atlas

Configuración centralizada para conectar a MongoDB Atlas
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from typing import Optional


class Database:
    """Singleton para la conexión a MongoDB"""
    
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None

    @classmethod
    async def connect(cls):
        """Conecta a MongoDB Atlas"""
        if cls.client is None:
            mongodb_uri = os.getenv("MONGODB_URI")
            
            if not mongodb_uri:
                raise ValueError("MONGODB_URI not found in environment variables")

            cls.client = AsyncIOMotorClient(
                mongodb_uri,
                maxPoolSize=10,
                minPoolSize=2,
            )
            
            # Nombre de la base de datos
            db_name = os.getenv("MONGODB_DB_NAME", "ufc_picks")
            cls.db = cls.client[db_name]

            # Test de conexión
            await cls.client.admin.command("ping")
            print(f"✅ Connected to MongoDB Atlas: {db_name}")

    @classmethod
    async def disconnect(cls):
        """Cierra la conexión"""
        if cls.client is not None:
            cls.client.close()
            cls.client = None
            cls.db = None
            print("❌ Disconnected from MongoDB")

    @classmethod
    def get_db(cls) -> AsyncIOMotorDatabase:
        """Retorna la instancia de la base de datos"""
        if cls.db is None:
            raise RuntimeError("Database not connected. Call Database.connect() first.")
        return cls.db


# ============================================
# 🎯 DEPENDENCY para FastAPI
# ============================================

async def get_database() -> AsyncIOMotorDatabase:
    """
    FastAPI dependency para inyectar la DB
    
    Uso:
        @app.get("/bouts/{bout_id}")
        async def get_bout(
            bout_id: int,
            db: AsyncIOMotorDatabase = Depends(get_database)
        ):
            repo = BoutRepository(db)
            return await repo.get_by_id(bout_id)
    """
    return Database.get_db()


# ============================================
# 🏗️ CREAR ÍNDICES (run once al deployment)
# ============================================

async def create_indexes():
    """
    Crea los índices necesarios para optimizar queries
    
    Llamar una vez al hacer deploy o en un script de inicialización
    """
    db = Database.get_db()

    # Índices para bouts
    await db.bouts.create_index("id", unique=True)
    await db.bouts.create_index("event_id")
    await db.bouts.create_index("status")
    await db.bouts.create_index([("event_id", 1), ("status", 1)])
    await db.bouts.create_index("fighters.red.fighter_name")
    await db.bouts.create_index("fighters.blue.fighter_name")

    # Índices para picks
    await db.picks.create_index("id", unique=True)
    await db.picks.create_index([("user_id", 1), ("bout_id", 1)], unique=True)
    await db.picks.create_index("event_id")
    await db.picks.create_index("bout_id")
    await db.picks.create_index([("user_id", 1), ("created_at", -1)])

    # Índices para events
    await db.events.create_index("id", unique=True)
    await db.events.create_index("status")
    await db.events.create_index("date")
    await db.events.create_index([("status", 1), ("date", 1)])

    # Índices para users
    await db.users.create_index("email", unique=True)
    await db.users.create_index("username", unique=True)

    # Índices para event_card_slots
    await db.event_card_slots.create_index("id", unique=True)
    await db.event_card_slots.create_index("event_id")
    await db.event_card_slots.create_index([("event_id", 1), ("order_overall", 1)])

    print("✅ Indexes created successfully")


# ============================================
# 🎯 EJEMPLO DE USO en main.py
# ============================================

"""
# backend/app/main.py

from fastapi import FastAPI
from app.database import Database, create_indexes

app = FastAPI()

@app.on_event("startup")
async def startup():
    await Database.connect()
    # await create_indexes()  # Solo la primera vez

@app.on_event("shutdown")
async def shutdown():
    await Database.disconnect()


# En tus routes:
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_database
from app.repositories import BoutRepository

@app.get("/bouts/{bout_id}")
async def get_bout(
    bout_id: int,
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    repo = BoutRepository(db)
    bout = await repo.get_by_id(bout_id)
    
    if not bout:
        raise HTTPException(status_code=404, detail="Bout not found")
    
    return bout
"""
