"""
Prepares a batch of places from the database for the restaurant-researcher agent.

Usage:
    python get_research_batch.py --limit 5
    python get_research_batch.py --place-id 2990864745
    python get_research_batch.py                # every place

Prints a JSON array of {place_id, name, lat, lon, website, amenity, cuisine}
in id order, one entry per place, meant to be handed one-per-agent to the
restaurant-researcher subagent for parallel batch research.
"""

import argparse
import json
import sqlite3

DB_PATH = "fedup.db"


def get_places(conn: sqlite3.Connection, limit: int | None, place_id: int | None):
    cur = conn.cursor()
    if place_id:
        cur.execute(
            "SELECT id, name, lat, lon, website, amenity, cuisine FROM places WHERE id = ?",
            (place_id,),
        )
    else:
        query = "SELECT id, name, lat, lon, website, amenity, cuisine FROM places ORDER BY id"
        if limit:
            query += f" LIMIT {limit}"
        cur.execute(query)
    return cur.fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a batch of places for the restaurant-researcher agent")
    parser.add_argument("--limit", type=int, help="Max number of places to include")
    parser.add_argument("--place-id", type=int, help="A single place by id")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    rows = get_places(conn, args.limit, args.place_id)
    conn.close()

    batch = [
        {
            "place_id": r[0],
            "name": r[1],
            "lat": r[2],
            "lon": r[3],
            "website": r[4],
            "amenity": r[5],
            "cuisine": r[6],
        }
        for r in rows
    ]
    print(json.dumps(batch, indent=2))


if __name__ == "__main__":
    main()
