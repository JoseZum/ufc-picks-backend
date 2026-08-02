"""
Unit tests for PickService
"""

import pytest
from datetime import datetime, timedelta, timezone

from app.services.pick_service import (
    PickService,
    PickLockedError,
    EventNotFoundError,
    BoutNotFoundError,
    InvalidPickError
)
from app.models.pick import PickCreate
from app.models.event import Event
from app.models.bout import Bout


class TestPickService:
    """Test suite for PickService business logic."""
    
    @pytest.mark.asyncio
    async def test_create_pick_success(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test successfully creating a pick."""
        # Setup: Insert event and bout
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        # Act
        pick = await service.create_or_update_pick("user123", pick_create)
        
        # Assert
        assert pick.user_id == "user123"
        assert pick.bout_id == sample_pick_data["bout_id"]
        assert pick.picked_fighter_name == "Test Fighter 1"
        assert pick.picked_method == "KO/TKO"
        assert pick.picked_round == 2
        assert pick.locked is False
    
    @pytest.mark.asyncio
    async def test_create_pick_event_not_found(self, test_db, sample_pick_data):
        """Test creating pick for non-existent event."""
        # Note: No event created in database, so get_by_id will return None
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        with pytest.raises(EventNotFoundError):
            await service.create_or_update_pick("user123", pick_create)
    
    @pytest.mark.asyncio
    async def test_create_pick_bout_not_found(self, test_db, sample_event_data, sample_pick_data):
        """Test creating pick for non-existent bout."""
        await test_db["events"].insert_one(sample_event_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        with pytest.raises(BoutNotFoundError):
            await service.create_or_update_pick("user123", pick_create)
    
    @pytest.mark.asyncio
    async def test_create_pick_for_completed_event(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test that picks cannot be created for completed events."""
        # Setup: Event is completed
        sample_event_data["status"] = "completed"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        with pytest.raises(PickLockedError):
            await service.create_or_update_pick("user123", pick_create)

    @pytest.mark.asyncio
    async def test_create_pick_for_completed_bout(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test that picks cannot be created for bouts already marked completed."""
        sample_bout_data["status"] = "completed"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)

        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)

        with pytest.raises(PickLockedError):
            await service.create_or_update_pick("user123", pick_create)

    @pytest.mark.asyncio
    async def test_create_pick_for_bout_with_registered_result(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test that picks cannot be created if the bout already has a result."""
        sample_bout_data["result"] = {
            "winner": "red",
            "method": "KO/TKO",
            "round": 2,
            "time": "3:45",
        }
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)

        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)

        with pytest.raises(PickLockedError):
            await service.create_or_update_pick("user123", pick_create)
    
    @pytest.mark.asyncio
    async def test_create_pick_with_admin_lock(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test that picks cannot be created when admin-locked."""
        # Setup: Event is admin-locked
        sample_event_data["picks_locked"] = True
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        with pytest.raises(PickLockedError):
            await service.create_or_update_pick("user123", pick_create)

    @pytest.mark.asyncio
    async def test_cannot_pick_after_bouts_section_has_started(
        self,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data,
    ):
        sample_event_data["section_lock_times_utc"] = {
            "prelim": datetime.now(timezone.utc) - timedelta(minutes=1),
            "main": datetime.now(timezone.utc) + timedelta(hours=2),
        }
        sample_bout_data["card_section"] = "prelim"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)

        service = PickService(test_db)
        with pytest.raises(PickLockedError, match="sección"):
            await service.create_or_update_pick(
                "user123",
                PickCreate(**sample_pick_data),
            )

    @pytest.mark.asyncio
    async def test_main_card_pick_stays_open_after_prelims_lock(
        self,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data,
    ):
        sample_event_data["section_lock_times_utc"] = {
            "prelim": datetime.now(timezone.utc) - timedelta(minutes=1),
            "main": datetime.now(timezone.utc) + timedelta(hours=2),
        }
        sample_bout_data["card_section"] = "main"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)

        pick = await PickService(test_db).create_or_update_pick(
            "user123",
            PickCreate(**sample_pick_data),
        )
        assert pick.bout_id == sample_bout_data["id"]

    @pytest.mark.asyncio
    async def test_admin_can_reopen_section_after_automatic_lock(
        self,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data,
    ):
        sample_event_data["section_lock_times_utc"] = {
            "prelim": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        sample_event_data["picks_lock_override"] = "unlocked"
        sample_bout_data["card_section"] = "prelim"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)

        pick = await PickService(test_db).create_or_update_pick(
            "user123",
            PickCreate(**sample_pick_data),
        )
        assert pick.bout_id == sample_bout_data["id"]
    
    @pytest.mark.asyncio
    async def test_update_existing_pick(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test updating an existing pick."""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        # Create initial pick
        pick1 = await service.create_or_update_pick("user123", pick_create)
        
        # Update pick
        pick_create.picked_fighter_name = "Test Fighter 2"
        pick_create.picked_method = "SUB"
        pick_create.picked_round = 3

        pick2 = await service.create_or_update_pick("user123", pick_create)

        # Assert
        assert pick2.id == pick1.id  # Same pick ID
        assert pick2.picked_fighter_name == "Test Fighter 2"
        assert pick2.picked_method == "SUB"
        assert pick2.picked_round == 3
        assert pick2.updated_at is not None

    @pytest.mark.asyncio
    async def test_cannot_change_a_mission_bound_pick_field(
        self,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data,
    ):
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        service = PickService(test_db)
        pick = await service.create_or_update_pick(
            "user123",
            PickCreate(**sample_pick_data),
        )
        await test_db["picks"].update_one(
            {"_id": pick.id},
            {
                "$set": {
                    "mission_field_locks": {
                        "winner": ["assignment-one"],
                        "method": ["assignment-one"],
                    }
                }
            },
        )

        with pytest.raises(PickLockedError, match="winner, method"):
            await service.create_or_update_pick(
                "user123",
                PickCreate(
                    event_id=sample_pick_data["event_id"],
                    bout_id=sample_pick_data["bout_id"],
                    picked_fighter_name="Test Fighter 2",
                    picked_method="SUB",
                    picked_round=2,
                ),
            )

    @pytest.mark.asyncio
    async def test_can_change_unbound_pick_fields_after_mission_selection(
        self,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data,
    ):
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        service = PickService(test_db)
        pick = await service.create_or_update_pick(
            "user123",
            PickCreate(**sample_pick_data),
        )
        await test_db["picks"].update_one(
            {"_id": pick.id},
            {"$set": {"mission_field_locks": {"winner": ["assignment-one"]}}},
        )

        updated = await service.create_or_update_pick(
            "user123",
            PickCreate(
                event_id=sample_pick_data["event_id"],
                bout_id=sample_pick_data["bout_id"],
                picked_fighter_name="Test Fighter 1",
                picked_method="SUB",
                picked_round=3,
            ),
        )

        assert updated.picked_fighter_name == "Test Fighter 1"
        assert updated.picked_method == "SUB"
        assert updated.picked_round == 3
    
    @pytest.mark.asyncio
    async def test_admin_unlock_can_reopen_legacy_locked_pick(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Effective event/bout state, not the legacy pick flag, controls edits."""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        # Create and lock pick
        pick = await service.create_or_update_pick("user123", pick_create)
        await test_db["picks"].update_one(
            {"_id": pick.id},
            {"$set": {"locked": True}}
        )
        
        await test_db["events"].update_one(
            {"id": sample_event_data["id"]},
            {
                "$set": {
                    "picks_locked": False,
                    "picks_lock_override": "unlocked",
                }
            },
        )
        pick_create.picked_fighter_name = "Test Fighter 2"
        updated = await service.create_or_update_pick("user123", pick_create)
        assert updated.picked_fighter_name == "Test Fighter 2"
    
    @pytest.mark.asyncio
    async def test_invalid_pick_round_for_dec(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test that DEC picks cannot have a round specified."""
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        
        # DEC with round should fail
        pick_create = PickCreate(
            event_id=sample_pick_data["event_id"],
            bout_id=sample_pick_data["bout_id"],
            picked_fighter_name="Test Fighter 1",
            picked_method="DEC",
            picked_round=5  # Invalid for DEC
        )

        with pytest.raises(InvalidPickError):
            await service.create_or_update_pick("user123", pick_create)
    
    @pytest.mark.asyncio
    async def test_normalize_name(self, test_db):
        """Test fighter name normalization."""
        service = PickService(test_db)

        assert service._normalize_name("Jon Jones") == "jon jones"
        assert service._normalize_name("  Khabib  Nurmagomedov  ") == "khabib nurmagomedov"
        assert service._normalize_name("CONOR MCGREGOR") == "conor mcgregor"
        assert service._normalize_name("") == ""

    @pytest.mark.asyncio
    async def test_get_user_picks_for_event(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test retrieving all user picks for an event."""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Create another bout for same event
        bout_2 = sample_bout_data.copy()
        bout_2["id"] = 67891
        # Remove _id so MongoDB generates a new one
        bout_2.pop("_id", None)
        await test_db["bouts"].insert_one(bout_2)
        
        service = PickService(test_db)
        
        # Create picks
        pick1 = PickCreate(**sample_pick_data)
        await service.create_or_update_pick("user123", pick1)
        
        pick2 = PickCreate(
            event_id=12345,
            bout_id=67891,
            picked_fighter_name="Test Fighter 2",
            picked_method="SUB",
            picked_round=1
        )
        await service.create_or_update_pick("user123", pick2)
        
        # Get picks
        picks = await service.get_user_picks_for_event("user123", 12345)
        
        assert len(picks) == 2
        assert {p.bout_id for p in picks} == {67890, 67891}
