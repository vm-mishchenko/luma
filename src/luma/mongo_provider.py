"""MongoDB implementation of EventProvider."""

from __future__ import annotations

from pymongo import DESCENDING, ReplaceOne
from pymongo.database import Database
from pymongo.errors import PyMongoError

from luma.event_store import CacheError
from luma.models import Event


class MongoEventProvider:
    """Reads and writes events in a MongoDB collection."""

    def __init__(self, database: Database, collection_name: str = "events") -> None:
        self._collection = database[collection_name]

    def load(self) -> list[Event]:
        try:
            docs = self._collection.find({}).sort("start_at", DESCENDING)
            events: list[Event] = []
            for doc in docs:
                doc.pop("_id", None)
                events.append(Event.model_validate(doc))
            return events
        except PyMongoError as err:
            raise CacheError(f"MongoDB read failed: {err}") from err

    def upsert(self, events: list[Event]) -> None:
        if not events:
            return
        try:
            ops = [
                ReplaceOne({"_id": e.id}, e.model_dump(), upsert=True)
                for e in events
            ]
            self._collection.bulk_write(ops, ordered=False)
        except PyMongoError as err:
            raise CacheError(f"MongoDB write failed: {err}") from err
