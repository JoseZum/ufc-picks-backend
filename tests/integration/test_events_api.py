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
        assert data[0]["poster_image_url"] == sample_event_data["poster_image_url"]
        assert data[0]["hero_image_url"] == sample_event_data["hero_image_url"]
    
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
        assert data["poster_image_url"] == sample_event_data["poster_image_url"]
        assert data["hero_image_url"] == sample_event_data["hero_image_url"]

    @pytest.mark.asyncio
    async def test_official_hero_fallback_is_exposed_as_card_poster(
        self, client, test_db, sample_event_data
    ):
        sample_event_data["poster_image_url"] = (
            "https://ufc.com/images/styles/background_image_xl_2x/s3/art.jpg"
        )
        sample_event_data["poster_image_source"] = "ufc_official_fallback"
        await test_db["events"].insert_one(sample_event_data)

        response = await client.get(f"/events/{sample_event_data['id']}")

        assert response.status_code == 200
        assert (
            response.json()["poster_image_url"]
            == sample_event_data["poster_image_url"]
        )
    
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

    @pytest.mark.asyncio
    async def test_bout_response_exposes_section_lock_state(
        self,
        client,
        test_db,
        sample_event_data,
        sample_bout_data,
    ):
        sample_event_data["section_lock_times_utc"] = {
            "prelim": datetime.now(timezone.utc),
        }
        sample_bout_data["card_section"] = "prelim"
        await test_db["events"].insert_one(sample_event_data)
        await test_db["bouts"].insert_one(sample_bout_data)

        response = await client.get(
            f"/events/{sample_event_data['id']}/bouts"
        )

        assert response.status_code == 200
        bout = response.json()[0]
        assert bout["card_section"] == "prelim"
        assert bout["effective_picks_locked"] is True
        assert bout["picks_lock_reason"] == "section_time"
        assert bout["automatic_lock_time_utc"] is not None
