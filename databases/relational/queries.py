"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
This module handles all queries to PostgreSQL.

TWO ROLES ARE SERVED HERE:
  1. Relational  → dual-network transit (metro + national rail),
                   availability, fares, bookings, seat selection
  2. Vector      → policy document similarity search (pgvector)

STUDENT TASK
------------
Design your schema in databases/relational/schema.sql, seed it with
skeleton/seed_postgres.py, then implement the query functions below.

Functions prefixed with `query_`  are read-only lookups called by the agent.
Functions prefixed with `execute_` are write operations (booking/cancellation).

The vector functions (query_policy_vector_search, store_policy_document)
are already implemented — do not modify them.
"""


from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from typing import Optional, Any

import psycopg2
import psycopg2.extras

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)



def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"BK-{suffix}"


def _gen_payment_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"PM-{suffix}"


# ============================================================================
#  PART 1: READ-ONLY OPERATIONS
# ============================================================================

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return national rail schedules that serve both origin and destination stations
    in the correct order, along with seat occupancy for the requested travel date.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        travel_date:     e.g. "2025-06-01" — used to count bookings; omit for general info
    """
    query = """
        SELECT 
            schedule_id, line, service_type, direction, origin_station_id, 
            destination_station_id, stops_in_order, passed_through_stations, 
            first_train_time, last_train_time, travel_time_from_origin_min, 
            standard_base_fare_usd, standard_per_stop_rate_usd, 
            first_base_fare_usd, first_per_stop_rate_usd, frequency_min, operates_on
        FROM national_rail_schedules
        WHERE %s = ANY(stops_in_order) 
          AND %s = ANY(stops_in_order)
    """
    params: list[Any] = [origin_id, destination_id]

    if travel_date:
        try:
            date_obj = datetime.strptime(travel_date, "%Y-%m-%d")
            day_of_week = date_obj.strftime("%a").lower()
            query += " AND %s = ANY(operates_on)"
            params.append(day_of_week)
        except ValueError:
            return []

    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, tuple(params))
            results = cur.fetchall()

            filtered_results = []
            for row in results:
                stops = row["stops_in_order"]
                if stops.index(origin_id) < stops.index(destination_id):
                    filtered_results.append(dict(row))

            if travel_date and filtered_results:
                schedule_ids = [r["schedule_id"] for r in filtered_results]

                cur.execute("""
                    SELECT schedule_id, COUNT(*) AS total_seats
                    FROM national_rail_seats
                    WHERE schedule_id = ANY(%s)
                    GROUP BY schedule_id
                """, (schedule_ids,))
                total_map = {r["schedule_id"]: r["total_seats"] for r in cur.fetchall()}

                cur.execute("""
                    SELECT schedule_id, COUNT(*) AS booked_seats
                    FROM national_rail_bookings
                    WHERE schedule_id = ANY(%s) AND travel_date = %s
                      AND status IN ('confirmed', 'completed')
                    GROUP BY schedule_id
                """, (schedule_ids, travel_date))
                booked_map = {r["schedule_id"]: r["booked_seats"] for r in cur.fetchall()}

                for r in filtered_results:
                    sid = r["schedule_id"]
                    total = int(total_map.get(sid, 0))
                    booked = int(booked_map.get(sid, 0))
                    r["total_seats"] = total
                    r["booked_seats"] = booked
                    r["available_seats"] = total - booked

            return filtered_results


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """
    Calculate the fare for a national rail journey.

    Args:
        schedule_id:     e.g. "NR_SCH01"
        fare_class:      "standard" or "first"
        stops_travelled: number of stops between origin and destination (inclusive)

    Returns:
        dict with fare_class, base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    query = """
        SELECT standard_base_fare_usd, standard_per_stop_rate_usd, 
               first_base_fare_usd, first_per_stop_rate_usd
        FROM national_rail_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (schedule_id,))
            row = cur.fetchone()
            if not row:
                return None

            if fare_class.lower() == 'first':
                base = float(row['first_base_fare_usd'])
                rate = float(row['first_per_stop_rate_usd'])
            else:
                base = float(row['standard_base_fare_usd'])
                rate = float(row['standard_per_stop_rate_usd'])

            total_fare = base + (rate * stops_travelled)
            return {
                "fare_class": fare_class.lower(),
                "base_fare_usd": base,
                "per_stop_rate_usd": rate,
                "total_fare_usd": round(total_fare, 2)
            }


def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Return metro schedules that serve both origin and destination in the correct order.

    Args:
        origin_id:       e.g. "MS01"
        destination_id:  e.g. "MS09"
    """
    query = """
        SELECT schedule_id, line, direction, origin_station_id, destination_station_id,
               stops_in_order, first_train_time, last_train_time, 
               travel_time_from_origin_min, base_fare_usd, per_stop_rate_usd, 
               frequency_min, operates_on
        FROM metro_schedules
        WHERE %s = ANY(stops_in_order) AND %s = ANY(stops_in_order)
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (origin_id, destination_id))
            results = cur.fetchall()
            
            filtered_results = []
            for row in results:
                stops = row["stops_in_order"]
                if stops.index(origin_id) < stops.index(destination_id):
                    filtered_results.append(dict(row))
            return filtered_results


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """
    Calculate the metro fare for a single-ticket journey.

    Args:
        schedule_id:     e.g. "MS_SCH01"
        stops_travelled: number of stops between origin and destination

    Returns:
        dict with base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    query = """
        SELECT base_fare_usd, per_stop_rate_usd
        FROM metro_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (schedule_id,))
            row = cur.fetchone()
            if not row:
                return None

            base = float(row['base_fare_usd'])
            rate = float(row['per_stop_rate_usd'])
            total_fare = base + (rate * stops_travelled)
            return {
                "base_fare_usd": base,
                "per_stop_rate_usd": rate,
                "total_fare_usd": round(total_fare, 2)
            }


def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Return available seats for a national rail journey on a given date.

    Args:
        schedule_id:  e.g. "NR_SCH01"
        travel_date:  e.g. "2025-06-01"
        fare_class:   "standard" or "first"

    Returns:
        List of dicts: {seat_id, coach, row, column}
    """
    fare_class = fare_class.lower()
    query = """
        SELECT s.seat_id, s.coach, s.fare_class, s.seat_row, s.seat_column
        FROM national_rail_seats s
        WHERE s.schedule_id = %s AND s.fare_class = %s
          AND s.seat_id NOT IN (
              SELECT b.seat_id 
              FROM national_rail_bookings b
              WHERE b.schedule_id = %s 
                AND b.travel_date = %s 
                AND b.status IN ('confirmed', 'completed')
          )
        ORDER BY s.coach, s.seat_row, s.seat_column
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (schedule_id, fare_class, schedule_id, travel_date))
            return [dict(row) for row in cur.fetchall()]


def auto_select_adjacent_seats(available_seats: list[dict], count: int) -> list[str]:
    """
    Select `count` seats that are as close together as possible (same row preferred,
    then adjacent rows). Returns a list of seat_ids.

    Args:
        available_seats: output of query_available_seats()
        count:           number of seats needed
    """
    if not available_seats or count <= 0:
        return []
    if count >= len(available_seats):
        return [s["seat_id"] for s in available_seats[:count]]

    from collections import defaultdict
    rows: dict[int, list[dict]] = defaultdict(list)
    for seat in available_seats:
        rows[seat["seat_row"]].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: s[0]["seat_row"]):
        if len(row_seats) >= count:
            return [s["seat_id"] for s in row_seats[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["seat_row"], s["seat_column"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    query = """
        SELECT user_id, full_name, email, phone, date_of_birth, registered_at, is_active
        FROM registered_users
        WHERE email = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (user_email,))
            row = cur.fetchone()
            return dict(row) if row else None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).

    Returns:
        dict with keys 'national_rail' (list) and 'metro' (list)
    """
    user_profile = query_user_profile(user_email)
    if not user_profile:
        return {"national_rail": [], "metro": []}

    user_id = user_profile["user_id"]

    rail_query = """
        SELECT booking_id, schedule_id, origin_station_id, destination_station_id,
               travel_date, ticket_type, fare_class, coach, seat_id,
               stops_travelled, amount_usd, status, booked_at
        FROM national_rail_bookings
        WHERE user_id = %s
        ORDER BY travel_date DESC, booked_at DESC
    """
    
    metro_query = """
        SELECT trip_id, schedule_id, origin_station_id, destination_station_id,
               travel_date, ticket_type, day_pass_ref, stops_travelled, amount_usd, status, travelled_at
        FROM metro_travel_history
        WHERE user_id = %s
        ORDER BY travel_date DESC, travelled_at DESC NULLS LAST
    """

    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(rail_query, (user_id,))
            rail_bookings = [dict(row) for row in cur.fetchall()]

            cur.execute(metro_query, (user_id,))
            metro_trips = [dict(row) for row in cur.fetchall()]

            return {
                "national_rail": rail_bookings,
                "metro": metro_trips
            }


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    query = """
        SELECT payment_id, national_rail_booking_id, metro_trip_id, amount_usd, method, status, paid_at
        FROM payments
        WHERE national_rail_booking_id = %s OR metro_trip_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (booking_id, booking_id))
            row = cur.fetchone()
            return dict(row) if row else None


# ============================================================================
#  PART 2: WRITE OPERATIONS (Transactions)
# ============================================================================

def execute_booking(
    user_id: str,
    schedule_id: str,
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    fare_class: str,
    seat_id: str,
    ticket_type: str = "single",
) -> tuple[bool, dict | str]:
    """
    Create a national rail booking for a logged-in user.

    Args:
        user_id:                e.g. "RU01" — must match the logged-in user
        schedule_id:            e.g. "NR_SCH01"
        origin_station_id:      e.g. "NR01"
        destination_station_id: e.g. "NR05"
        travel_date:            e.g. "2025-06-01"
        fare_class:             "standard" or "first"
        seat_id:                e.g. "B05" (or "any" to auto-assign)
        ticket_type:            "single" (default) or "return"

    Returns:
        (True, booking_dict)   on success
        (False, error_message) on failure
    """
    fare_class = fare_class.lower()
    if seat_id.lower() == 'any':
        available = query_available_seats(schedule_id, travel_date, fare_class)
        if not available:
            return False, "No seats available for this journey."
        seat_id = auto_select_adjacent_seats(available, 1)[0]

    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            lock_query = """
                SELECT coach FROM national_rail_seats
                WHERE schedule_id = %s AND seat_id = %s
                FOR UPDATE
            """
            cur.execute(lock_query, (schedule_id, seat_id))
            seat_row = cur.fetchone()
            if not seat_row:
                return False, f"Seat {seat_id} does not exist on this schedule."
            coach = seat_row["coach"]

            booked_query = """
                SELECT 1 FROM national_rail_bookings
                WHERE schedule_id = %s AND travel_date = %s AND seat_id = %s
                AND status IN ('confirmed', 'completed')
            """
            cur.execute(booked_query, (schedule_id, travel_date, seat_id))
            if cur.fetchone():
                return False, f"Seat {seat_id} is already occupied on {travel_date}."

            schedule_query = """
                SELECT stops_in_order, standard_base_fare_usd, standard_per_stop_rate_usd,
                       first_base_fare_usd, first_per_stop_rate_usd, first_train_time
                FROM national_rail_schedules WHERE schedule_id = %s
            """
            cur.execute(schedule_query, (schedule_id,))
            sched = cur.fetchone()
            if not sched:
                return False, "Target schedule not found."

            stops: list = sched["stops_in_order"]
            if origin_station_id not in stops or destination_station_id not in stops:
                return False, "Invalid origin or destination for this line."

            idx_origin = stops.index(origin_station_id)
            idx_dest = stops.index(destination_station_id)
            if idx_origin >= idx_dest:
                return False, "Invalid routing order."

            stops_travelled = idx_dest - idx_origin

            if fare_class == 'first':
                base = float(sched['first_base_fare_usd'])
                rate = float(sched['first_per_stop_rate_usd'])
            else:
                base = float(sched['standard_base_fare_usd'])
                rate = float(sched['standard_per_stop_rate_usd'])
            amount_usd = base + (rate * stops_travelled)

            booking_id = _gen_booking_id()
            insert_booking = """
                INSERT INTO national_rail_bookings (
                    booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                    travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                    stops_travelled, amount_usd, status, booked_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING *
            """
            cur.execute(insert_booking, (
                booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                travel_date, sched["first_train_time"], ticket_type, fare_class, coach, seat_id,
                stops_travelled, amount_usd, 'confirmed'
            ))
            new_booking = dict(cur.fetchone())

            pay_id = _gen_payment_id()
            insert_payment = """
                INSERT INTO payments (payment_id, national_rail_booking_id, metro_trip_id, amount_usd, method, status, paid_at)
                VALUES (%s, %s, NULL, %s, 'credit_card', 'paid', NOW())
            """
            cur.execute(insert_payment, (pay_id, booking_id, amount_usd))

        conn.commit()
        return True, new_booking

    except Exception as e:
        conn.rollback()
        return False, f"Database transaction aborted: {str(e)}"
    finally:
        conn.close()


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.

    Calculates the refund amount according to the booking's service type:
      - Normal service: RF001 windows (100% / 75% / 50% / 0%)
      - Express service: RF002 windows (100% / 50% / 0%)

    Args:
        booking_id: e.g. "BK001"
        user_id:    must match the booking's user_id

    Returns:
        (True, result_dict)  with refund_amount_usd and policy note
        (False, error_msg)
    """
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT b.status, b.amount_usd, b.travel_date, b.departure_time,
                       s.service_type
                FROM national_rail_bookings b
                JOIN national_rail_schedules s ON b.schedule_id = s.schedule_id
                WHERE b.booking_id = %s AND b.user_id = %s
                FOR UPDATE OF b
            """, (booking_id, user_id))
            booking = cur.fetchone()

            if not booking:
                return False, "Booking not found or access denied."
            if booking["status"] in ("cancelled", "refunded"):
                return False, "Booking is already cancelled or refunded."

            # Calculate hours until departure
            departure_dt = datetime.combine(booking["travel_date"], booking["departure_time"])
            hours_until = (departure_dt - datetime.now()).total_seconds() / 3600

            amount = float(booking["amount_usd"])
            service_type = (booking["service_type"] or "normal").lower()

            # Apply refund policy
            if service_type == "express":
                # RF002
                if hours_until >= 48:
                    refund_pct, admin_fee, policy_note = 1.0, 1.00, "RF002_W1: 100% refund (>=48h), admin fee $1.00"
                elif hours_until >= 24:
                    refund_pct, admin_fee, policy_note = 0.5, 1.00, "RF002_W2: 50% refund (24-48h), admin fee $1.00"
                else:
                    refund_pct, admin_fee, policy_note = 0.0, 0.00, "RF002_W3: No refund (<24h)"
            else:
                # RF001 (normal)
                if hours_until >= 48:
                    refund_pct, admin_fee, policy_note = 1.0, 0.00, "RF001_W1: 100% refund (>=48h), no admin fee"
                elif hours_until >= 24:
                    refund_pct, admin_fee, policy_note = 0.75, 0.50, "RF001_W2: 75% refund (24-48h), admin fee $0.50"
                elif hours_until >= 2:
                    refund_pct, admin_fee, policy_note = 0.5, 0.50, "RF001_W3: 50% refund (2-24h), admin fee $0.50"
                else:
                    refund_pct, admin_fee, policy_note = 0.0, 0.00, "RF001_W4: No refund (<2h)"

            refund_amount = round(max(amount * refund_pct - admin_fee, 0), 2)

            cur.execute("""
                UPDATE national_rail_bookings SET status = 'cancelled'
                WHERE booking_id = %s RETURNING *
            """, (booking_id,))
            updated_booking = dict(cur.fetchone())

            cur.execute("""
                UPDATE payments SET status = 'refunded' WHERE national_rail_booking_id = %s
            """, (booking_id,))

        conn.commit()
        return True, {
            **updated_booking,
            "refund_amount_usd": refund_amount,
            "policy_note": policy_note,
        }

    except Exception as e:
        conn.rollback()
        return False, f"Cancellation failed: {str(e)}"
    finally:
        conn.close()


# ============================================================================
#  PART 3: AUTHENTICATION & SECURITY
# ============================================================================

def register_user(
    email: str,
    first_name: str,
    surname: str,
    year_of_birth: int,
    password: str,
    secret_question: str,
    secret_answer: str,
) -> tuple[bool, str]:
    """
    Register a new user.
    Returns (True, user_id) on success or (False, error_message) on failure.

    NOTE: passwords are stored as plain text here intentionally for teaching
    purposes. In production, replace with a salted hash (e.g. bcrypt).
    """
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            user_id = f"U{''.join(random.choices(string.ascii_uppercase + string.digits, k=7))}"
            dob_string = f"{year_of_birth}-01-01"

            insert_profile = """
                INSERT INTO registered_users (user_id, full_name, email, date_of_birth, registered_at)
                VALUES (%s, %s, %s, %s, NOW())
            """
            cur.execute(insert_profile, (user_id, f"{first_name} {surname}", email, dob_string))

            insert_cred = """
                INSERT INTO user_credentials (user_id, password_hash, secret_question, secret_answer_hash)
                VALUES (%s, %s, %s, %s)
            """
            cur.execute(insert_cred, (user_id, ph.hash(password), secret_question, ph.hash(secret_answer)))

        conn.commit()
        return True, user_id
    except psycopg2.IntegrityError:
        conn.rollback()
        return False, "A user account with this email already exists."
    except Exception as e:
        conn.rollback()
        return False, f"Registration pipeline error: {str(e)}"
    finally:
        conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials. Returns a user dict on success or None on failure.
    Dict keys: user_id, email, full_name, first_name, surname, phone, date_of_birth, is_active.
    """
    query = """
    SELECT u.user_id, u.full_name, u.email, u.phone, u.date_of_birth, u.is_active,
           c.password_hash
    FROM registered_users u
    JOIN user_credentials c ON u.user_id = c.user_id
    WHERE u.email = %s AND u.is_active = TRUE
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (email,))
            row = cur.fetchone()
            if not row:
                return None

            try:
                ph.verify(row["password_hash"], password)
            except VerifyMismatchError:
                return None

            result = dict(row)
            del result["password_hash"]
            name_parts = result["full_name"].split(" ", 1)
            result["first_name"] = name_parts[0]
            result["surname"] = name_parts[1] if len(name_parts) > 1 else ""
            return result


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email, or None if not found."""
    query = """
        SELECT c.secret_question 
        FROM user_credentials c
        JOIN registered_users u ON u.user_id = c.user_id
        WHERE u.email = %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (email,))
            row = cur.fetchone()
            return row[0] if row else None


def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored secret answer (case-insensitive)."""
    query = """
    SELECT c.secret_answer_hash FROM user_credentials c
    JOIN registered_users u ON u.user_id = c.user_id
    WHERE u.email = %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (email,))
            row = cur.fetchone()
            if not row:
                return False
            try:
                ph.verify(row[0], answer)
                return True
            except VerifyMismatchError:
                return False


def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user. Returns True if the row was updated."""
    query = """
        UPDATE user_credentials 
        SET password_hash = %s, last_password_change = NOW()
        WHERE user_id = (SELECT user_id FROM registered_users WHERE email = %s)
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (ph.hash(new_password), email))
            return cur.rowcount > 0


# ============================================================================
#  PART 4: VECTOR OPERATIONS (RAG / Help Desk Lookup) — do not modify
# ============================================================================

def query_policy_vector_search(
    embedding: list[float],
    top_k: int = VECTOR_TOP_K,
) -> list[dict]:
    """
    Perform a cosine similarity vector search against policy_documents.
    Returns documents with similarity score above VECTOR_SIMILARITY_THRESHOLD.

    Used by skeleton/agent.py to answer helpdesk questions. Do not modify.

    Returns:
        List of dicts with title, category, content, and similarity score
    """
    sql = """
        SELECT
            title,
            category,
            content,
            1 - (embedding <=> %s::vector) AS similarity
        FROM policy_documents
        WHERE 1 - (embedding <=> %s::vector) > %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (vec_str, vec_str, VECTOR_SIMILARITY_THRESHOLD, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]


def store_policy_document(
    title: str,
    category: str,
    content: str,
    embedding: list[float],
    source_file: str = "",
) -> int:
    """
    Insert a policy document with its embedding into the database.
    Used by skeleton/seed_vectors.py — students don't need to call this directly.

    Returns:
        The new document's id
    """
    sql = """
        INSERT INTO policy_documents (title, category, content, embedding, source_file)
        VALUES (%s, %s, %s, %s::vector, %s)
        RETURNING id
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (title, category, content, vec_str, source_file))
            new_id = cur.fetchone()[0]
            return new_id