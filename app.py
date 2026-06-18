import json
import sqlite3

import requests
from flask import Flask, g, jsonify, render_template

app = Flask(__name__)
DB_PATH = "fedup.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def reverse_geocode(lat, lon):
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": "FedUp/1.0"},
            timeout=5,
        )
        return resp.json().get("display_name", "")
    except Exception:
        return ""


def parse_menu(text):
    """Convert scraped menu text into a list of {section, items} dicts."""
    if not text or "enable JavaScript" in text:
        return []

    import re
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    price_re = re.compile(r"^\$?\d+(\.\d+)?$")

    sections = []
    current_section = None
    current_items = []
    i = 0

    while i < len(lines):
        line = lines[i]
        # A line followed by items/prices is likely a section header —
        # detect by checking if it has no price and is short
        if (
            not price_re.match(line)
            and len(line) < 50
            and i + 1 < len(lines)
            and not price_re.match(lines[i + 1])
            and line == line.title()  # Title Case = section header heuristic
        ):
            if current_section is not None:
                sections.append({"section": current_section, "items": current_items})
            current_section = line
            current_items = []
            i += 1
            continue

        # Collect item name + optional description + price
        name = line
        desc = ""
        price = ""
        j = i + 1
        # Look ahead for a price on one of the next 3 lines
        while j < min(i + 4, len(lines)):
            if price_re.match(lines[j]):
                price = lines[j]
                desc = " ".join(lines[i + 1 : j]) if j > i + 1 else ""
                i = j + 1
                break
            j += 1
        else:
            i += 1

        if current_section is not None:
            current_items.append({"name": name, "desc": desc, "price": price})

    if current_section is not None:
        sections.append({"section": current_section, "items": current_items})

    return sections


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/places")
def list_places():
    rows = get_db().execute(
        """SELECT id, name, cuisine, amenity, rating, phone, website,
                  deals_text IS NOT NULL AND deals_text != '' AS has_deals,
                  (scraped_menu_text LIKE '%event%' OR deals_text LIKE '%event%') AS has_events
           FROM places ORDER BY name"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/places/<int:place_id>")
def get_place(place_id):
    row = get_db().execute(
        "SELECT * FROM places WHERE id = ?", (place_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    data = dict(row)
    data["address"] = reverse_geocode(data["lat"], data["lon"])
    data["menu_sections"] = parse_menu(data.get("scraped_menu_text"))

    # Parse hours JSON string
    if data.get("hours"):
        try:
            data["hours"] = json.loads(data["hours"])
        except Exception:
            pass

    return jsonify(data)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
