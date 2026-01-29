"""
Integration tests for Picks API endpoints
"""

import pytest


class TestPicksEndpoints:
    """Test suite for /picks endpoints."""
    
    @pytest.mark.asyncio
    async def test_create_pick_authenticated(
        self,
        client,
        auth_headers,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data
    ):
        """Test POST /picks with authentication"""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Act
        response = await client.post(
            "/picks",
            json=sample_pick_data,
            headers=auth_headers
        )
        
        # Assert
        assert response.status_code == 201
        data = response.json()
        assert data["bout_id"] == sample_pick_data["bout_id"]
        assert data["picked_corner"] == sample_pick_data["picked_corner"]
        assert data["picked_method"] == sample_pick_data["picked_method"]
    
    @pytest.mark.asyncio
    async def test_create_pick_unauthenticated(self, client, sample_pick_data):
        """Test POST /picks without authentication"""
        response = await client.post("/picks", json=sample_pick_data)
        assert response.status_code == 403  # FastAPI HTTPBearer returns 403 when no token
    
    @pytest.mark.asyncio
    async def test_get_user_picks_for_event(
        self,
        client,
        auth_headers,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data,
        sample_user_data
    ):
        """Test GET /picks/event/{event_id}"""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Create pick first
        await client.post(
            "/picks",
            json=sample_pick_data,
            headers=auth_headers
        )
        
        # Act
        response = await client.get(
            f"/picks/me?event_id={sample_event_data['id']}",
            headers=auth_headers
        )
        
        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["event_id"] == sample_event_data["id"]
    
    @pytest.mark.asyncio
    async def test_update_existing_pick(
        self,
        client,
        auth_headers,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data
    ):
        """Test updating an existing pick"""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Create initial pick
        response1 = await client.post(
            "/picks",
            json=sample_pick_data,
            headers=auth_headers
        )
        assert response1.status_code == 201
        
        # Update pick
        updated_pick = sample_pick_data.copy()
        updated_pick["picked_corner"] = "blue"
        updated_pick["picked_method"] = "SUB"
        
        response2 = await client.post(
            "/picks",
            json=updated_pick,
            headers=auth_headers
        )
        
        # Assert
        assert response2.status_code == 201
        data = response2.json()
        assert data["picked_corner"] == "blue"
        assert data["picked_method"] == "SUB"
    
    @pytest.mark.asyncio
    async def test_cannot_create_pick_for_completed_event(
        self,
        client,
        auth_headers,
        test_db,
        sample_event_data,
        sample_bout_data,
        sample_pick_data
    ):
        """Test that picks cannot be created for completed events"""
        # Setup: completed event
        sample_event_data["status"] = "completed"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Act
        response = await client.post(
            "/picks",
            json=sample_pick_data,
            headers=auth_headers
        )
        
        # Assert
        assert response.status_code == 403  # PickLockedError returns 403 Forbidden
        detail = response.json()["detail"].lower()
        assert "completed" in detail or "cancelled" in detail
