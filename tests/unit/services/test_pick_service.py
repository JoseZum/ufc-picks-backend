"""
Unit tests for PickService
"""

import pytest
from datetime import datetime, timezone

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
        assert pick.picked_corner == "red"
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
        pick_create.picked_corner = "blue"
        pick_create.picked_method = "SUB"
        pick_create.picked_round = 3
        
        pick2 = await service.create_or_update_pick("user123", pick_create)
        
        # Assert
        assert pick2._id == pick1._id  # Same pick ID
        assert pick2.picked_corner == "blue"
        assert pick2.picked_method == "SUB"
        assert pick2.picked_round == 3
        assert pick2.updated_at is not None
    
    @pytest.mark.asyncio
    async def test_cannot_update_locked_pick(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test that locked picks cannot be updated."""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        service = PickService(test_db)
        pick_create = PickCreate(**sample_pick_data)
        
        # Create and lock pick
        pick = await service.create_or_update_pick("user123", pick_create)
        await test_db["picks"].update_one(
            {"_id": pick._id},
            {"$set": {"locked": True}}
        )
        
        # Try to update
        pick_create.picked_corner = "blue"
        
        with pytest.raises(PickLockedError):
            await service.create_or_update_pick("user123", pick_create)
    
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
            picked_corner="red",
            picked_method="DEC",
            picked_round=5  # Invalid for DEC
        )
        
        with pytest.raises(InvalidPickError):
            await service.create_or_update_pick("user123", pick_create)
    
    @pytest.mark.asyncio
    async def test_calculate_score_correct_fighter_only(self, test_db):
        """Test scoring: correct fighter only = 1 point."""
        service = PickService(test_db)
        
        is_correct, points = await service.calculate_score(
            picked_corner="red",
            picked_method="KO/TKO",
            picked_round=2,
            result={"winner": "red", "method": "SUB", "round": 3}
        )
        
        assert is_correct is True
        assert points == 1
    
    @pytest.mark.asyncio
    async def test_calculate_score_correct_fighter_and_method(self, test_db):
        """Test scoring: correct fighter + method = 2 points."""
        service = PickService(test_db)
        
        is_correct, points = await service.calculate_score(
            picked_corner="red",
            picked_method="KO/TKO",
            picked_round=2,
            result={"winner": "red", "method": "KO/TKO", "round": 3}
        )
        
        assert is_correct is True
        assert points == 2
    
    @pytest.mark.asyncio
    async def test_calculate_score_perfect_pick(self, test_db):
        """Test scoring: correct fighter + method + round = 3 points."""
        service = PickService(test_db)
        
        is_correct, points = await service.calculate_score(
            picked_corner="red",
            picked_method="KO/TKO",
            picked_round=2,
            result={"winner": "red", "method": "KO/TKO", "round": 2}
        )
        
        assert is_correct is True
        assert points == 3
    
    @pytest.mark.asyncio
    async def test_calculate_score_wrong_fighter(self, test_db):
        """Test scoring: wrong fighter = 0 points."""
        service = PickService(test_db)
        
        is_correct, points = await service.calculate_score(
            picked_corner="red",
            picked_method="KO/TKO",
            picked_round=2,
            result={"winner": "blue", "method": "KO/TKO", "round": 2}
        )
        
        assert is_correct is False
        assert points == 0
    
    @pytest.mark.asyncio
    async def test_calculate_score_dec_correct(self, test_db):
        """Test scoring: DEC picks max 2 points."""
        service = PickService(test_db)
        
        is_correct, points = await service.calculate_score(
            picked_corner="red",
            picked_method="DEC",
            picked_round=None,
            result={"winner": "red", "method": "DEC", "round": 5}
        )
        
        assert is_correct is True
        assert points == 2
    
    @pytest.mark.asyncio
    async def test_normalize_method(self, test_db):
        """Test method normalization."""
        service = PickService(test_db)
        
        assert service._normalize_method("KO") == "KO/TKO"
        assert service._normalize_method("TKO") == "KO/TKO"
        assert service._normalize_method("Submission") == "SUB"
        assert service._normalize_method("Decision") == "DEC"
        assert service._normalize_method("") == "DEC"
    
    @pytest.mark.asyncio
    async def test_get_user_picks_for_event(self, test_db, sample_event_data, sample_bout_data, sample_pick_data):
        """Test retrieving all user picks for an event."""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Create another bout for same event
        bout_2 = sample_bout_data.copy()
        bout_2["id"] = 67891
        await test_db["bouts"].insert_one(bout_2)
        
        service = PickService(test_db)
        
        # Create picks
        pick1 = PickCreate(**sample_pick_data)
        await service.create_or_update_pick("user123", pick1)
        
        pick2 = PickCreate(
            event_id=12345,
            bout_id=67891,
            picked_corner="blue",
            picked_method="SUB",
            picked_round=1
        )
        await service.create_or_update_pick("user123", pick2)
        
        # Get picks
        picks = await service.get_user_picks_for_event("user123", 12345)
        
        assert len(picks) == 2
        assert {p.bout_id for p in picks} == {67890, 67891}
