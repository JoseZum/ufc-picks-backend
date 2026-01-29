"""
Integration tests for Events API endpoints
"""

import pytest
from datetime import datetime, timezone


class TestEventsEndpoints:
    """Test suite for /events endpoints."""
    
    @pytest.mark.asyncio
    async def test_get_upcoming_events(self, client, test_db, sample_event_data):
        """Test GET /events?status=scheduled"""
        # Setup: Insert scheduled event
        await test_db["events"].insert_one(sample_event_data)
        
        # Act
        response = await client.get("/events?status=scheduled")
        
        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["id"] == sample_event_data["id"]
        assert data[0]["name"] == sample_event_data["name"]
    
    @pytest.mark.asyncio
    async def test_get_event_by_id(self, client, test_db, sample_event_data):
        """Test GET /events/{event_id}"""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        
        # Act
        response = await client.get(f"/events/{sample_event_data['id']}")
        
        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_event_data["id"]
        assert data["name"] == sample_event_data["name"]
        assert data["status"] == "scheduled"
    
    @pytest.mark.asyncio
    async def test_get_event_not_found(self, client):
        """Test GET /events/{event_id} with non-existent event"""
        response = await client.get("/events/999999")
        assert response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_get_event_bouts(self, client, test_db, sample_event_data, sample_bout_data):
        """Test GET /events/{event_id}/bouts"""
        # Setup
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)
        
        # Act
        response = await client.get(f"/events/{sample_event_data['id']}/bouts")
        
        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["id"] == sample_bout_data["id"]
        assert data[0]["event_id"] == sample_event_data["id"]
