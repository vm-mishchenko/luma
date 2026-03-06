"""Shared domain models for luma."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Host(BaseModel):
    name: str = Field(description="Host or organizer name")
    linkedin_handle: str | None = Field(default=None, description="Host's LinkedIn handle, if available")
    youtube_handle: str | None = Field(default=None, description="Host's YouTube handle, if available")


class Event(BaseModel):
    id: str = Field(description="Unique Luma event identifier (starts with evt-)")
    title: str = Field(description="Event title as displayed on Luma")
    url: str = Field(description="Full URL to the event page on luma.com")
    start_at: str = Field(description="Event start time in ISO 8601 UTC format")
    guest_count: int = Field(description="Number of users who RSVP'd to this event")
    sources: list[str] = Field(default_factory=list, description="Data sources this event was discovered from, e.g. 'category:ai', 'calendar:sf-tech'")
    location_type: str | None = Field(default=None, description="Whether the event is 'offline' (in-person) or 'online' (virtual)")
    latitude: float | None = Field(default=None, description="Venue latitude for offline events")
    longitude: float | None = Field(default=None, description="Venue longitude for offline events")
    city: str | None = Field(default=None, description="City where the event takes place")
    region: str | None = Field(default=None, description="State or region where the event takes place")
    country: str | None = Field(default=None, description="Country where the event takes place")
    hosts: list[Host] = Field(default_factory=list, description="Event hosts or organizers")


class Category(BaseModel):
    api_id: str = Field(description="Category identifier, e.g. 'cat-ai'")
    name: str = Field(description="Display name, e.g. 'AI'")
    slug: str = Field(description="URL slug, e.g. 'ai'")


class EventDetail(BaseModel):
    event_id: str = Field(description="Luma event ID (starts with evt-)")
    description_md: str = Field(description="Event description in markdown")
    categories: list[Category] = Field(description="Event categories")
