"""
Configuración de la conexión a MongoDB - el corazón de la BD
"""

from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import get_settings

settings = get_settings()


class Database:
    """Singleton para la conexión a MongoDB con su pool de conexiones"""

    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None

    @classmethod
    async def connect(cls):
        """Conecta a MongoDB Atlas y establece el pool de conexiones"""
        if cls.client is None:
            # maxPoolSize y minPoolSize para evitar abrir demasiadas conexiones
            cls.client = AsyncIOMotorClient(
                settings.mongodb_uri,
                maxPoolSize=10,
                minPoolSize=2,
            )

            cls.db = cls.client[settings.mongodb_db_name]

            # Test de conexión para verificar que todo está bien
            await cls.client.admin.command("ping")
            print(f"[OK] Conectado a MongoDB: {settings.mongodb_db_name}")

    @classmethod
    async def disconnect(cls):
        """Cierra la conexión cuando la app se apaga"""
        if cls.client is not None:
            cls.client.close()
            cls.client = None
            cls.db = None
            print("[OK] Desconectado de MongoDB")

    @classmethod
    def get_db(cls) -> AsyncIOMotorDatabase:
        """Retorna la instancia de la BD (para usar en las repositories)"""
        if cls.db is None:
            raise RuntimeError("BD no conectada. Llama Database.connect() primero.")
        return cls.db


async def get_database() -> AsyncIOMotorDatabase:
    """Dependency para inyectar la BD en los endpoints"""
    return Database.get_db()


async def create_indexes():
    """
    Crea índices en las colecciones para optimizar queries
    
    Esto se ejecuta una sola vez en el deployment/inicialización
    Los índices mejoran el performance de búsquedas sin cambiar el código
    """
    db = Database.get_db()

    # Índices para Users
    await db.users.create_index("email", unique=True)
    await db.users.create_index("google_id", unique=True)

    # Índices para Events - filtro por status y fecha es muy común
    await db.events.create_index("id", unique=True)
    await db.events.create_index("status")
    await db.events.create_index("date")
    await db.events.create_index([("status", 1), ("date", 1)])

    # Índices para Bouts - muchas búsquedas por evento
    await db.bouts.create_index("id", unique=True)
    await db.bouts.create_index("event_id")
    await db.bouts.create_index("status")
    await db.bouts.create_index([("event_id", 1), ("status", 1)])
    # Búsquedas de peleadores dentro de nested fields
    await db.bouts.create_index("fighters.red.fighter_name")
    await db.bouts.create_index("fighters.blue.fighter_name")

    # Índices para Picks - la combinación user_id:bout_id es única
    await db.picks.create_index("_id", unique=True)
    await db.picks.create_index([("user_id", 1), ("bout_id", 1)], unique=True)
    await db.picks.create_index("event_id")
    await db.picks.create_index("bout_id")
    # Para traer todos los picks de un usuario en un evento
    await db.picks.create_index([("user_id", 1), ("event_id", 1)])

    # Índices para Leaderboards - filtro por categoría y scope
    await db.leaderboards.create_index([("category", 1), ("scope", 1)])
    # Para ranking por puntos
    await db.leaderboards.create_index([("category", 1), ("scope", 1), ("total_points", -1)])
    await db.leaderboards.create_index("user_id")

    # Índices para la estructura de carteleras
    await db.event_card_slots.create_index("id", unique=True)
    await db.event_card_slots.create_index("event_id")
    # Para traer el orden de peleas en un evento
    await db.event_card_slots.create_index([("event_id", 1), ("order_overall", 1)])

    print("[OK] Indices de BD creados correctamente")
