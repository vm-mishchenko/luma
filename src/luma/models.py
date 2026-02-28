"""Shared domain models for luma."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Host:
    name: str
    linkedin_handle: str | None = None
    youtube_handle: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "linkedin_handle": self.linkedin_handle,
            "youtube_handle": self.youtube_handle,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Host:
        return Host(
            name=d["name"],
            linkedin_handle=d.get("linkedin_handle"),
            youtube_handle=d.get("youtube_handle"),
        )


@dataclass
class Event:
    id: str
    title: str
    url: str
    start_at: str
    guest_count: int
    sources: list[str] = field(default_factory=list)
    location_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    hosts: list[Host] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "start_at": self.start_at,
            "guest_count": self.guest_count,
            "sources": self.sources,
            "location_type": self.location_type,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "city": self.city,
            "region": self.region,
            "country": self.country,
            "hosts": [h.to_dict() for h in self.hosts],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Event:
        hosts_raw = d.get("hosts", [])
        return Event(
            id=d["id"],
            title=d["title"],
            url=d["url"],
            start_at=d["start_at"],
            guest_count=d["guest_count"],
            sources=d.get("sources", []),
            location_type=d.get("location_type"),
            latitude=d.get("latitude"),
            longitude=d.get("longitude"),
            city=d.get("city"),
            region=d.get("region"),
            country=d.get("country"),
            hosts=[Host.from_dict(h) for h in hosts_raw],
        )
