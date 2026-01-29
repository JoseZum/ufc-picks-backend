"""
Unit tests for PointsService
"""

import pytest
from datetime import datetime, timezone

from app.services.points_service import PointsService


class TestPointsService:
    """Test suite for PointsService scoring logic."""
    
    @pytest.mark.asyncio
    async def test_normalize_method(self, test_db):
        """Test method normalization."""
        service = PointsService(test_db)
        
        assert service.normalize_method("KO") == "KO/TKO"
        assert service.normalize_method("TKO") == "KO/TKO"
        assert service.normalize_method("KO/TKO") == "KO/TKO"
        assert service.normalize_method("SUB") == "SUB"
        assert service.normalize_method("SUBMISSION") == "SUB"
        assert service.normalize_method("DEC") == "DEC"
        assert service.normalize_method("DECISION") == "DEC"
    
    @pytest.mark.asyncio
    async def test_calculate_points_perfect_pick(self, test_db):
        """Test perfect pick: fighter + method + round = 3 points."""
        service = PointsService(test_db)
        
        pick = {
            "picked_corner": "red",
            "picked_method": "KO/TKO",
            "picked_round": 2
        }
        
        result = {
            "winner": "red",
            "method": "KO",
            "round": 2
        }
        
        points = await service.calculate_points(pick, result)
        assert points == 3
    
    @pytest.mark.asyncio
    async def test_calculate_points_fighter_and_method(self, test_db):
        """Test correct fighter and method = 2 points."""
        service = PointsService(test_db)
        
        pick = {
            "picked_corner": "red",
            "picked_method": "KO/TKO",
            "picked_round": 2
        }
        
        result = {
            "winner": "red",
            "method": "KO",
            "round": 3  # Different round
        }
        
        points = await service.calculate_points(pick, result)
        assert points == 2
    
    @pytest.mark.asyncio
    async def test_calculate_points_fighter_only(self, test_db):
        """Test correct fighter only = 1 point."""
        service = PointsService(test_db)
        
        pick = {
            "picked_corner": "red",
            "picked_method": "KO/TKO",
            "picked_round": 2
        }
        
        result = {
            "winner": "red",
            "method": "SUB",  # Different method
            "round": 3
        }
        
        points = await service.calculate_points(pick, result)
        assert points == 1
    
    @pytest.mark.asyncio
    async def test_calculate_points_wrong_fighter(self, test_db):
        """Test wrong fighter = 0 points."""
        service = PointsService(test_db)
        
        pick = {
            "picked_corner": "red",
            "picked_method": "KO/TKO",
            "picked_round": 2
        }
        
        result = {
            "winner": "blue",  # Wrong fighter
            "method": "KO",
            "round": 2
        }
        
        points = await service.calculate_points(pick, result)
        assert points == 0
    
    @pytest.mark.asyncio
    async def test_calculate_points_draw(self, test_db):
        """Test draw result = 0 points."""
        service = PointsService(test_db)
        
        pick = {
            "picked_corner": "red",
            "picked_method": "DEC",
            "picked_round": None
        }
        
        result = {
            "winner": None,  # Draw
            "method": "DEC",
            "round": 5
        }
        
        points = await service.calculate_points(pick, result)
        assert points == 0
    
    @pytest.mark.asyncio
    async def test_calculate_points_no_round_specified(self, test_db):
        """Test pick without round can still get points."""
        service = PointsService(test_db)
        
        pick = {
            "picked_corner": "red",
            "picked_method": "KO/TKO",
            "picked_round": None  # No round specified
        }
        
        result = {
            "winner": "red",
            "method": "KO",
            "round": 2
        }
        
        points = await service.calculate_points(pick, result)
        assert points == 2  # Fighter + method, no round bonus
    
    @pytest.mark.asyncio
    async def test_calculate_and_assign_points(self, test_db, sample_bout_data, sample_result_data):
        """Test calculating and assigning points to all picks for a bout."""
        # Setup: Create picks
        await test_db["bouts"].insert_one(sample_bout_data)
        
        picks = [
            {
                "_id": "user1:67890",
                "user_id": "user1",
                "bout_id": 67890,
                "picked_corner": "red",
                "picked_method": "KO/TKO",
                "picked_round": 2
            },
            {
                "_id": "user2:67890",
                "user_id": "user2",
                "bout_id": 67890,
                "picked_corner": "red",
                "picked_method": "SUB",
                "picked_round": 1
            },
            {
                "_id": "user3:67890",
                "user_id": "user3",
                "bout_id": 67890,
                "picked_corner": "blue",
                "picked_method": "KO/TKO",
                "picked_round": 2
            }
        ]
        
        await test_db["picks"].insert_many(picks)
        
        # Setup: Create users
        for i in range(1, 4):
            await test_db["users"].insert_one({
                "_id": f"user{i}",
                "name": f"User {i}",
                "total_points": 0,
                "picks_total": 0,
                "picks_correct": 0,
                "perfect_picks": 0,
                "accuracy": 0.0
            })
        
        service = PointsService(test_db)
        
        # Act
        result = await service.calculate_and_assign_points(67890, sample_result_data)
        
        # Assert
        assert result["picks_processed"] == 3
        assert result["points_distributed"] == 4  # 3 + 1 + 0
        assert result["users_affected"] == 3
        
        # Check individual picks
        pick1 = await test_db["picks"].find_one({"_id": "user1:67890"})
        assert pick1["points_awarded"] == 3  # Perfect pick
        assert pick1["is_correct"] is True
        
        pick2 = await test_db["picks"].find_one({"_id": "user2:67890"})
        assert pick2["points_awarded"] == 1  # Fighter only
        assert pick2["is_correct"] is True
        
        pick3 = await test_db["picks"].find_one({"_id": "user3:67890"})
        assert pick3["points_awarded"] == 0  # Wrong fighter
        assert pick3["is_correct"] is False
    
    @pytest.mark.asyncio
    async def test_revert_points(self, test_db):
        """Test reverting points for a bout."""
        # Setup: Create picks with points
        picks = [
            {
                "_id": "user1:67890",
                "user_id": "user1",
                "bout_id": 67890,
                "picked_corner": "red",
                "picked_method": "KO/TKO",
                "picked_round": 2,
                "points_awarded": 3,
                "is_correct": True
            },
            {
                "_id": "user2:67890",
                "user_id": "user2",
                "bout_id": 67890,
                "picked_corner": "blue",
                "picked_method": "SUB",
                "picked_round": 1,
                "points_awarded": 0,
                "is_correct": False
            }
        ]
        
        await test_db["picks"].insert_many(picks)
        
        # Setup: Create users with stats
        await test_db["users"].insert_many([
            {
                "_id": "user1",
                "name": "User 1",
                "total_points": 10,
                "picks_total": 5,
                "picks_correct": 3,
                "perfect_picks": 1,
                "accuracy": 0.6
            },
            {
                "_id": "user2",
                "name": "User 2",
                "total_points": 5,
                "picks_total": 5,
                "picks_correct": 2,
                "perfect_picks": 0,
                "accuracy": 0.4
            }
        ])
        
        service = PointsService(test_db)
        
        # Act
        await service.revert_points(67890)
        
        # Assert: picks should be reset
        pick1 = await test_db["picks"].find_one({"_id": "user1:67890"})
        assert pick1["points_awarded"] == 0
        assert pick1["is_correct"] is None
        
        pick2 = await test_db["picks"].find_one({"_id": "user2:67890"})
        assert pick2["points_awarded"] == 0
        assert pick2["is_correct"] is None
    
    @pytest.mark.asyncio
    async def test_update_user_stats(self, test_db):
        """Test updating user statistics based on picks."""
        # Setup: Create user
        await test_db["users"].insert_one({
            "_id": "user1",
            "name": "Test User",
            "total_points": 0,
            "picks_total": 0,
            "picks_correct": 0,
            "perfect_picks": 0,
            "accuracy": 0.0
        })
        
        # Setup: Create picks for user
        picks = [
            {
                "_id": "user1:1",
                "user_id": "user1",
                "bout_id": 1,
                "points_awarded": 3,
                "is_correct": True
            },
            {
                "_id": "user1:2",
                "user_id": "user1",
                "bout_id": 2,
                "points_awarded": 2,
                "is_correct": True
            },
            {
                "_id": "user1:3",
                "user_id": "user1",
                "bout_id": 3,
                "points_awarded": 1,
                "is_correct": True
            },
            {
                "_id": "user1:4",
                "user_id": "user1",
                "bout_id": 4,
                "points_awarded": 0,
                "is_correct": False
            }
        ]
        
        await test_db["picks"].insert_many(picks)
        
        service = PointsService(test_db)
        
        # Act
        await service._update_user_stats("user1")
        
        # Assert
        user = await test_db["users"].find_one({"_id": "user1"})
        assert user["total_points"] == 6  # 3 + 2 + 1 + 0
        assert user["picks_total"] == 4
        assert user["picks_correct"] == 3  # 3 correct out of 4
        assert user["perfect_picks"] == 1  # Only 1 with 3 points
        assert user["accuracy"] == 0.75  # 3/4 = 0.75
