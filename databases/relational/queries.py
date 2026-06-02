"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
Handles core data access operations for both metro and national rail networks,
including availability queries, fare calculation, booking transactions, and authentication.
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


def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    """Generate a unique tracking identifier for rail bookings."""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"BK-{suffix}"


# ============================================================================
#  PART 1: READ-ONLY OPERATIONS
# ============================================================================

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Find available train schedules between two stations with correct routing sequence.

    Args:
        origin_id (str): Starting rail station identifier.
        destination_id (str): Destination rail station identifier.
        travel_date (Optional[str]): Target date string in YYYY-MM-DD format.

    Returns:
        list[dict]: Array of matching rows from national_rail_schedules, or [] if none found.
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
            day_of_week = date_obj.strftime("%A")
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
            return filtered_results


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """
    Calculate fares dynamically based on stops travelled and seat tier classification.

    Args:
        schedule_id (str): National rail schedule identification string.
        fare_class (str): Chosen passenger class tier ('standard' or 'first').
        stops_travelled (int): Sequence delta count between origin and destination.

    Returns:
        Optional[dict]: Calculated fare breakdowns, or None if schedule does not exist.
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
                "base_fare": base,
                "per_stop_rate": rate,
                "total_fare": round(total_fare, 2)
            }


def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Retrieve active metro line schedules configured between designated nodes.

    Args:
        origin_id (str): Transit gateway boarding point identifier.
        destination_id (str): Transit gateway alighting point identifier.

    Returns:
        list[dict]: Array of matched schedules configured in sequential direction.
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
    Calculate metro flat rate increments based on station traversal delta counts.

    Args:
        schedule_id (str): Target line schedule code token.
        stops_travelled (int): Net number of segments crossed.

    Returns:
        Optional[dict]: Computed base, per-stop metrics, and aggregated total.
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
                "base_fare": base,
                "per_stop_rate": rate,
                "total_fare": round(total_fare, 2)
            }


def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Filter inventory allocation maps to isolate unreserved physical seats.

    Args:
        schedule_id (str): Targeted transit line index.
        travel_date (str): Iso formatted date matrix.
        fare_class (str): Seat configuration class tier filter.

    Returns:
        list[dict]: Available physical layout properties array.
    """
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


def query_user_profile(user_email: str) -> Optional[dict]:
    """
    Obtain primary profile entity attributes via unique identity email.

    Args:
        user_email (str): Target account authentication mail alias.

    Returns:
        Optional[dict]: Core identity components mapping or None.
    """
    query = """
        SELECT user_id, first_name, surname, email, phone, date_of_birth, registered_at, is_active
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
    Compile historical ledger components across distinct multi-modal networks.

    Args:
        user_email (str): Target client validation mail token.

    Returns:
        dict: Aggregated ledger containing 'national_rail' and 'metro' branches.
    """
    user_profile = query_user_profile(user_email)
    if not user_profile:
        return {"national_rail": [], "metro": []}

    user_id = user_profile["user_id"]

    rail_query = """
        SELECT booking_id, schedule_id, origin_station_id, destination_station_id,
               travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
               stops_travelled, amount_usd, status, booked_at
        FROM national_rail_bookings
        WHERE user_id = %s
        ORDER BY travel_date DESC, departure_time DESC
    """
    
    metro_query = """
        SELECT trip_id, schedule_id, origin_station_id, destination_station_id,
               travel_date, ticket_type, day_pass_ref, stops_travelled, amount_usd, status, travelled_at
        FROM metro_travel_history
        WHERE user_id = %s
        ORDER BY travel_date DESC, travelled_at DESC
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
    """
    Examine financial transactions ledger to locate records linked to specific tokens.

    Args:
        booking_id (str): Core verification hash code string.

    Returns:
        Optional[dict]: Payment audit attributes trace row metadata mapping.
    """
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
    Execute booking flow by managing reservation updates and transactions safely.

    Args:
        user_id (str): Customer entity primary identifier token.
        schedule_id (str): Targeted route transit run identifier.
        origin_station_id (str): Initial boarding station locator.
        destination_station_id (str): Final terminal arrival station locator.
        travel_date (str): Manifest placement date constraint index.
        fare_class (str): Target service cabin standard tier parameter string.
        seat_id (str): Spatial asset structural deployment position string.
        ticket_type (str): Ticket purchase variant default configuration string.

    Returns:
        tuple[bool, dict | str]: Isolation status outcome binary wrapper with context information.
    """
    with _connect() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                check_seat_query = """
                    SELECT 1 FROM national_rail_bookings
                    WHERE schedule_id = %s AND travel_date = %s AND seat_id = %s AND status IN ('confirmed', 'completed')
                    FOR UPDATE
                """
                cur.execute(check_seat_query, (schedule_id, travel_date, seat_id))
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

                if fare_class.lower() == 'first':
                    base = float(sched['first_base_fare_usd'])
                    rate = float(sched['first_per_stop_rate_usd'])
                else:
                    base = float(sched['standard_base_fare_usd'])
                    rate = float(sched['standard_per_stop_rate_usd'])
                amount_usd = base + (rate * stops_travelled)

                seat_query = "SELECT coach FROM national_rail_seats WHERE schedule_id = %s AND seat_id = %s"
                cur.execute(seat_query, (schedule_id, seat_id))
                seat_row = cur.fetchone()
                coach = seat_row["coach"] if seat_row else "C1"

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

                pay_id = f"PAY-{int(datetime.now().timestamp())}"
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


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel active manifest seats and coordinate transaction reversal.

    Args:
        booking_id (str): Core target ledger item.
        user_id (str): Verifiable owner profile verification code string.

    Returns:
        tuple[bool, dict | str]: State updates outcome tracking indicator payload.
    """
    with _connect() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT status FROM national_rail_bookings 
                    WHERE booking_id = %s AND user_id = %s FOR UPDATE
                """, (booking_id, user_id))
                booking = cur.fetchone()
                
                if not booking:
                    return False, "Booking not found or access denied."
                if booking["status"] == "cancelled" or booking["status"] == "refunded":
                    return False, "Booking is already cancelled or refunded."

                cur.execute("""
                    UPDATE national_rail_bookings SET status = 'cancelled' 
                    WHERE booking_id = %s RETURNING *
                """, (booking_id,))
                updated_booking = dict(cur.fetchone())

                cur.execute("""
                    UPDATE payments SET status = 'refunded' WHERE national_rail_booking_id = %s
                """, (booking_id,))

                conn.commit()
                return True, updated_booking
        except Exception as e:
            conn.rollback()
            return False, f"Cancellation failed: {str(e)}"


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
    Provision systemic identity credentials across database allocation maps.

    Args:
        email (str): Target unique entry communication handle string.
        first_name (str): Core name parameter metadata.
        surname (str): Secondary identity text notation format parameter.
        year_of_birth (int): Numerical year converted into standard ISO calendar string.
        password (str): Target credentials password hash value string.
        secret_question (str): Backup restoration security textual component template.
        secret_answer (str): Validation key target parameter validation string.

    Returns:
        tuple[bool, str]: Completion monitoring tracking code outcome.
    """
    with _connect() as conn:
        try:
            with conn.cursor() as cur:
                user_id = f"U{int(datetime.now().timestamp()) % 10000000:07d}"
                dob_string = f"{year_of_birth}-01-01"

                insert_profile = """
                    INSERT INTO registered_users (user_id, first_name, surname, email, date_of_birth, registered_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                """
                cur.execute(insert_profile, (user_id, first_name, surname, email, dob_string))

                insert_cred = """
                    INSERT INTO user_credentials (user_id, password_hash, secret_question, secret_answer_hash)
                    VALUES (%s, %s, %s, %s)
                """
                cur.execute(insert_cred, (user_id, password, secret_question, secret_answer))

                conn.commit()
                return True, user_id
        except psycopg2.IntegrityError:
            conn.rollback()
            return False, "A user account with this email already exists."
        except Exception as e:
            conn.rollback()
            return False, f"Registration pipeline error: {str(e)}"


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Authenticate inbound customer signatures against stored profile structures.

    Args:
        email (str): Validation email token target.
        password (str): Input signature validation entity hash parameter.

    Returns:
        Optional[dict]: Isolated target validation fields profile mapping block or None.
    """
    query = """
        SELECT u.user_id, u.first_name, u.surname, u.email, u.is_active
        FROM registered_users u
        JOIN user_credentials c ON u.user_id = c.user_id
        WHERE u.email = %s AND c.password_hash = %s AND u.is_active = TRUE
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (email, password))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_secret_question(email: str) -> Optional[str]:
    """
    Acquire recovery challenges mapped to target account labels.
    """
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
    """
    Evaluate validity of security response configurations.
    """
    query = """
        SELECT 1 
        FROM user_credentials c
        JOIN registered_users u ON u.user_id = c.user_id
        WHERE u.email = %s AND c.secret_answer_hash = %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (email, answer))
            return cur.fetchone() is not None


def update_password(email: str, new_password: str) -> bool:
    """
    Modify operational system entity secret records safely.
    """
    query = """
        UPDATE user_credentials 
        SET password_hash = %s, last_password_change = NOW()
        WHERE user_id = (SELECT user_id FROM registered_users WHERE email = %s)
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (new_password, email))
            conn.commit()
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