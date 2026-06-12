"""
Seed PostgreSQL with all TransitFlow mock data from train-mock-data/.

Usage:
    python skeleton/seed_postgres.py

Run AFTER docker-compose up -d.
You must first design and create your tables in databases/relational/schema.sql.
Safe to re-run: implement your inserts with ON CONFLICT DO NOTHING.
"""

import json
import os
import sys

import psycopg2
from psycopg2.extras import execute_values
from argon2 import PasswordHasher

# ── resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR    = os.path.join(PROJECT_DIR, "train-mock-data")

sys.path.insert(0, PROJECT_DIR)
from skeleton import config as cfg


def load(filename):
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def connect():
    return psycopg2.connect(
        host=cfg.PG_HOST,
        port=cfg.PG_PORT,
        dbname=cfg.PG_DB,
        user=cfg.PG_USER,
        password=cfg.PG_PASSWORD,
    )


def insert_many(cur, table, columns, rows):
    """Bulk insert with ON CONFLICT DO NOTHING. Returns row count inserted."""
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT DO NOTHING"
    )
    execute_values(cur, sql, rows)
    return cur.rowcount


# ── seeders ──────────────────────────────────────────────────────────────────

def seed_metro_stations(cur):
    data = load("metro_stations.json")
    rows = []
    for s in data:
        rows.append((
            s.get("station_id"),
            s.get("name"),
            s.get("lines"),
            s.get("is_interchange_metro", False),
            s.get("is_interchange_national_rail", False),
            s.get("interchange_national_rail_station_id"),
        ))
    inserted = insert_many(cur, "metro_stations",
                           ["station_id", "name", "lines", "is_interchange_metro", "is_interchange_national_rail", "interchange_national_rail_station_id"],
                           rows)
    print(f"  metro_stations: inserted {inserted}")


def seed_national_rail_stations(cur):
    data = load("national_rail_stations.json")
    rows = []
    for s in data:
        rows.append((
            s.get("station_id"),
            s.get("name"),
            s.get("lines"),
            s.get("is_interchange_national_rail", False),
            s.get("is_interchange_metro", False),
            s.get("interchange_metro_station_id"),
        ))
    inserted = insert_many(cur, "national_rail_stations",
                           ["station_id", "name", "lines", "is_interchange_national_rail", "is_interchange_metro", "interchange_metro_station_id"],
                           rows)
    print(f"  national_rail_stations: inserted {inserted}")


def seed_metro_schedules(cur):
    data = load("metro_schedules.json")
    rows = []
    for s in data:
        rows.append((
            s.get("schedule_id"),
            s.get("line"),
            s.get("direction"),
            s.get("origin_station_id"),
            s.get("destination_station_id"),
            s.get("stops_in_order"),
            s.get("first_train_time"),
            s.get("last_train_time"),
            json.dumps(s.get("travel_time_from_origin_min", {})),
            s.get("base_fare_usd"),
            s.get("per_stop_rate_usd"),
            s.get("frequency_min"),
            s.get("operates_on"),
        ))
    inserted = insert_many(cur, "metro_schedules",
                           ["schedule_id", "line", "direction", "origin_station_id", "destination_station_id", "stops_in_order", "first_train_time", "last_train_time", "travel_time_from_origin_min", "base_fare_usd", "per_stop_rate_usd", "frequency_min", "operates_on"],
                           rows)
    print(f"  metro_schedules: inserted {inserted}")


def seed_national_rail_schedules(cur):
    data = load("national_rail_schedules.json")
    rows = []
    for s in data:
        fare_classes = s.get("fare_classes", {})
        standard = fare_classes.get("standard", {})
        first = fare_classes.get("first", {})
        rows.append((
            s.get("schedule_id"),
            s.get("line"),
            s.get("service_type"),
            s.get("direction"),
            s.get("origin_station_id"),
            s.get("destination_station_id"),
            s.get("stops_in_order"),
            s.get("passed_through_stations"),
            s.get("first_train_time"),
            s.get("last_train_time"),
            json.dumps(s.get("travel_time_from_origin_min", {})),
            standard.get("base_fare_usd"),
            standard.get("per_stop_rate_usd"),
            first.get("base_fare_usd"),
            first.get("per_stop_rate_usd"),
            s.get("frequency_min"),
            s.get("operates_on"),
        ))
    inserted = insert_many(cur, "national_rail_schedules",
                           ["schedule_id", "line", "service_type", "direction", "origin_station_id", "destination_station_id", "stops_in_order", "passed_through_stations", "first_train_time", "last_train_time", "travel_time_from_origin_min", "standard_base_fare_usd", "standard_per_stop_rate_usd", "first_base_fare_usd", "first_per_stop_rate_usd", "frequency_min", "operates_on"],
                           rows)
    print(f"  national_rail_schedules: inserted {inserted}")


def seed_seat_layouts(cur):
    data = load("national_rail_seat_layouts.json")
    rows = []
    for layout in data:
        schedule_id = layout.get("schedule_id")
        for coach in layout.get("coaches", []):
            coach_id = coach.get("coach")
            fare_class = coach.get("fare_class")
            for seat in coach.get("seats", []):
                rows.append((
                    schedule_id,
                    seat.get("seat_id"),
                    coach_id,
                    fare_class,
                    seat.get("row"),
                    seat.get("column"),
                ))
    inserted = insert_many(cur, "national_rail_seats",
                           ["schedule_id", "seat_id", "coach", "fare_class", "seat_row", "seat_column"],
                           rows)
    print(f"  national_rail_seats: inserted {inserted}")


def seed_users(cur):
    data = load("registered_users.json")
    user_rows = []
    cred_rows = []
    ph = PasswordHasher()
    for u in data:
        user_id = u.get("user_id")
        full_name = u.get("full_name")
        user_rows.append((
            user_id,
            full_name,
            u.get("email"),
            u.get("phone"),
            u.get("date_of_birth"),
            u.get("registered_at"),
            u.get("is_active", True),
        ))

        # Hash password and secret answer using argon2 (argon2-cffi)
        pwd = u.get("password") or ""
        try:
            pwd_hash = ph.hash(pwd)
        except Exception:
            pwd_hash = None
        secret_answer = (u.get("secret_answer") or "").lower()
        try:
            secret_hash = ph.hash(secret_answer)
        except Exception:
            secret_hash = None
        cred_rows.append((
            user_id,
            pwd_hash,
            u.get("secret_question"),
            secret_hash,
        ))

    inserted_users = insert_many(cur, "registered_users",
                                 ["user_id", "full_name", "email", "phone", "date_of_birth", "registered_at", "is_active"],
                                 user_rows)
    print(f"  registered_users: inserted {inserted_users}")

    inserted_creds = insert_many(cur, "user_credentials",
                                 ["user_id", "password_hash", "secret_question", "secret_answer_hash"],
                                 cred_rows)
    print(f"  user_credentials: inserted {inserted_creds}")


def seed_national_rail_bookings(cur):
    data = load("bookings.json")
    rows = []
    for b in data:
        rows.append((
            b.get("booking_id"),
            b.get("user_id"),
            b.get("schedule_id"),
            b.get("origin_station_id"),
            b.get("destination_station_id"),
            b.get("travel_date"),
            b.get("departure_time"),
            b.get("ticket_type"),
            b.get("fare_class"),
            b.get("coach"),
            b.get("seat_id"),
            b.get("stops_travelled"),
            b.get("amount_usd"),
            b.get("status"),
            b.get("booked_at"),
            b.get("travelled_at"),
        ))
    inserted = insert_many(cur, "national_rail_bookings",
                           ["booking_id", "user_id", "schedule_id", "origin_station_id", "destination_station_id", "travel_date", "departure_time", "ticket_type", "fare_class", "coach", "seat_id", "stops_travelled", "amount_usd", "status", "booked_at", "travelled_at"],
                           rows)
    print(f"  national_rail_bookings: inserted {inserted}")


def seed_metro_travels(cur):
    data = load("metro_travel_history.json")
    rows = []
    for t in data:
        rows.append((
            t.get("trip_id"),
            t.get("user_id"),
            t.get("schedule_id"),
            t.get("origin_station_id"),
            t.get("destination_station_id"),
            t.get("travel_date"),
            t.get("ticket_type"),
            t.get("day_pass_ref"),
            t.get("stops_travelled"),
            t.get("amount_usd"),
            t.get("status"),
            t.get("purchased_at"),
            t.get("travelled_at"),
        ))
    inserted = insert_many(cur, "metro_travel_history",
                           ["trip_id", "user_id", "schedule_id", "origin_station_id", "destination_station_id", "travel_date", "ticket_type", "day_pass_ref", "stops_travelled", "amount_usd", "status", "purchased_at", "travelled_at"],
                           rows)
    print(f"  metro_travel_history: inserted {inserted}")


def seed_payments(cur):
    data = load("payments.json")
    rows = []
    for p in data:
        booking_ref = p.get("booking_id") or p.get("booking")
        # determine whether booking_ref is a national rail booking (BK...) or metro trip (MT...)
        national_rail_booking_id = None
        metro_trip_id = None
        if booking_ref:
            if str(booking_ref).upper().startswith("BK"):
                national_rail_booking_id = booking_ref
            else:
                metro_trip_id = booking_ref

        rows.append((
            p.get("payment_id"),
            national_rail_booking_id,
            metro_trip_id,
            p.get("amount_usd"),
            p.get("method"),
            p.get("status"),
            p.get("paid_at"),
        ))
    inserted = insert_many(cur, "payments",
                           ["payment_id", "national_rail_booking_id", "metro_trip_id", "amount_usd", "method", "status", "paid_at"],
                           rows)
    print(f"  payments: inserted {inserted}")


def seed_feedback(cur):
    data = load("feedback.json")
    rows = []
    for f in data:
        booking_ref = f.get("booking_id")
        national_rail_booking_id = None
        metro_trip_id = None
        if booking_ref:
            if str(booking_ref).upper().startswith("BK"):
                national_rail_booking_id = booking_ref
            else:
                metro_trip_id = booking_ref

        rows.append((
            f.get("feedback_id"),
            national_rail_booking_id,
            metro_trip_id,
            f.get("user_id"),
            f.get("rating"),
            f.get("comment"),
            f.get("submitted_at"),
        ))
    inserted = insert_many(cur, "feedback",
                           ["feedback_id", "national_rail_booking_id", "metro_trip_id", "user_id", "rating", "comment", "submitted_at"],
                           rows)
    print(f"  feedback: inserted {inserted}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to PostgreSQL...")
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("Seeding tables (dependency order):")
        seed_metro_stations(cur)
        seed_national_rail_stations(cur)
        seed_metro_schedules(cur)
        seed_national_rail_schedules(cur)
        seed_seat_layouts(cur)
        seed_users(cur)
        seed_national_rail_bookings(cur)
        seed_metro_travels(cur)
        seed_payments(cur)
        seed_feedback(cur)
        conn.commit()
        print("\nAll done. Database seeded successfully.")
    except Exception as e:
        conn.rollback()
        print(f"\nError: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
