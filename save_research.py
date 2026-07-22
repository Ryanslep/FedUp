"""
Upserts restaurant-researcher agent output into the places table.

Usage:
    python save_research.py --place-id ID --json '{"cuisine": "greek", "phone": "..."}'
    python save_research.py --place-id ID --json-file data.json
    echo '{"cuisine": "greek"}' | python save_research.py --place-id ID
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = "fedup.db"

ALLOWED_FIELDS = {
    "name", "address", "phone", "website", "menu_url", "cuisine", "hours",
    "rating", "review_count", "deals_text", "scraped_menu_text",
    "events_text",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Save researched restaurant fields to the places table")
    parser.add_argument("--place-id", type=int, required=True)
    parser.add_argument("--json", help="JSON object of fields to update")
    parser.add_argument("--json-file", help="path to a JSON file of fields to update")
    args = parser.parse_args()

    if args.json_file:
        with open(args.json_file, encoding="utf-8") as f:
            data = json.load(f)
    elif args.json:
        data = json.loads(args.json)
    else:
        data = json.load(sys.stdin)

    data = {k: v for k, v in data.items() if k in ALLOWED_FIELDS and v not in (None, "")}
    if not data:
        print("[skip] no recognized fields to save")
        return

    data["last_scraped"] = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH, timeout=15)
    cur = conn.cursor()
    cur.execute("SELECT id FROM places WHERE id = ?", (args.place_id,))
    if not cur.fetchone():
        print(f"[error] no place with id {args.place_id}")
        conn.close()
        sys.exit(1)

    cols = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE places SET {cols} WHERE id = ?", [*data.values(), args.place_id])
    conn.commit()
    conn.close()
    print(f"[saved] place {args.place_id}: {list(data.keys())}")


if __name__ == "__main__":
    main()
