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
from typing import Optional

import psycopg2
import psycopg2.extras

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD


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


# ── Example ───────────────────────────────────────────────────────────────────

def example_query() -> dict:
    """Example: returns the name of the connected database."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS db;")
            return dict(cur.fetchone())


# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

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
    # 找出同時包含 origin 和 destination 且 origin 在前的班次
    # array_position 回傳 1-based 位置；若不存在回傳 NULL
    sql = """
        SELECT
            s.schedule_id,
            s.line,
            s.service_type,
            s.direction,
            s.origin_station_id,
            s.destination_station_id,
            s.stops_in_order,
            s.first_train_time,
            s.last_train_time,
            s.frequency_min,
            s.operates_on,
            s.standard_base_fare_usd,
            s.standard_per_stop_rate_usd,
            s.first_base_fare_usd,
            s.first_per_stop_rate_usd,
            array_position(s.stops_in_order, %s) AS origin_pos,
            array_position(s.stops_in_order, %s) AS destination_pos,
            (array_position(s.stops_in_order, %s) - array_position(s.stops_in_order, %s)) AS stops_travelled,
            (
                SELECT COUNT(*)
                FROM national_rail_bookings b
                WHERE b.schedule_id = s.schedule_id
                  AND b.status = 'confirmed'
                  AND (%s::DATE IS NULL OR b.travel_date = %s::DATE)
            ) AS booked_seats_count,
            (
                SELECT COUNT(*)
                FROM national_rail_seats ns
                WHERE ns.schedule_id = s.schedule_id
            ) AS total_seats_count
        FROM national_rail_schedules s
        WHERE
            %s = ANY(s.stops_in_order)
            AND %s = ANY(s.stops_in_order)
            AND array_position(s.stops_in_order, %s) < array_position(s.stops_in_order, %s)
        ORDER BY s.line, s.service_type
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (
                origin_id, destination_id,          # origin_pos, destination_pos
                destination_id, origin_id,          # stops_travelled 計算
                travel_date, travel_date,           # booked_seats_count subquery
                origin_id, destination_id,          # WHERE ANY conditions
                origin_id, destination_id,          # array_position order check
            ))
            return [dict(row) for row in cur.fetchall()]


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
    sql = """
        SELECT
            schedule_id,
            service_type,
            CASE
                WHEN %s = 'first' THEN first_base_fare_usd
                ELSE standard_base_fare_usd
            END AS base_fare_usd,
            CASE
                WHEN %s = 'first' THEN first_per_stop_rate_usd
                ELSE standard_per_stop_rate_usd
            END AS per_stop_rate_usd
        FROM national_rail_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (fare_class, fare_class, schedule_id))
            row = cur.fetchone()
            if row is None:
                return None
            row = dict(row)
            total = float(row["base_fare_usd"]) + float(row["per_stop_rate_usd"]) * stops_travelled
            return {
                "schedule_id": row["schedule_id"],
                "service_type": row["service_type"],
                "fare_class": fare_class,
                "base_fare_usd": float(row["base_fare_usd"]),
                "per_stop_rate_usd": float(row["per_stop_rate_usd"]),
                "stops_travelled": stops_travelled,
                "total_fare_usd": round(total, 2),
            }


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Return metro schedules that serve both origin and destination in the correct order.

    Args:
        origin_id:       e.g. "MS01"
        destination_id:  e.g. "MS09"
    """
    sql = """
        SELECT
            schedule_id,
            line,
            direction,
            origin_station_id,
            destination_station_id,
            stops_in_order,
            first_train_time,
            last_train_time,
            frequency_min,
            operates_on,
            base_fare_usd,
            per_stop_rate_usd,
            array_position(stops_in_order, %s) AS origin_pos,
            array_position(stops_in_order, %s) AS destination_pos,
            (array_position(stops_in_order, %s) - array_position(stops_in_order, %s)) AS stops_travelled
        FROM metro_schedules
        WHERE
            %s = ANY(stops_in_order)
            AND %s = ANY(stops_in_order)
            AND array_position(stops_in_order, %s) < array_position(stops_in_order, %s)
        ORDER BY line, direction
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (
                origin_id, destination_id,      # origin_pos, destination_pos
                destination_id, origin_id,      # stops_travelled
                origin_id, destination_id,      # WHERE ANY
                origin_id, destination_id,      # order check
            ))
            return [dict(row) for row in cur.fetchall()]


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """
    Calculate the metro fare for a single-ticket journey.

    Args:
        schedule_id:     e.g. "MS_SCH01"
        stops_travelled: number of stops between origin and destination

    Returns:
        dict with base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    sql = """
        SELECT schedule_id, base_fare_usd, per_stop_rate_usd
        FROM metro_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id,))
            row = cur.fetchone()
            if row is None:
                return None
            row = dict(row)
            total = float(row["base_fare_usd"]) + float(row["per_stop_rate_usd"]) * stops_travelled
            return {
                "schedule_id": row["schedule_id"],
                "base_fare_usd": float(row["base_fare_usd"]),
                "per_stop_rate_usd": float(row["per_stop_rate_usd"]),
                "stops_travelled": stops_travelled,
                "total_fare_usd": round(total, 2),
            }


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

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
    # 所有該艙等的座位，排除當天已 confirmed 的訂位
    sql = """
        SELECT
            ns.seat_id,
            ns.coach,
            ns.seat_row   AS row,
            ns.seat_column AS column
        FROM national_rail_seats ns
        WHERE ns.schedule_id = %s
          AND ns.fare_class = %s
          AND NOT EXISTS (
              SELECT 1
              FROM national_rail_bookings b
              WHERE b.schedule_id = ns.schedule_id
                AND b.seat_id     = ns.seat_id
                AND b.travel_date = %s::DATE
                AND b.status      = 'confirmed'
          )
        ORDER BY ns.seat_row, ns.seat_column
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id, fare_class, travel_date))
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
        rows[seat["row"]].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: s[0]["row"]):
        if len(row_seats) >= count:
            return [s["seat_id"] for s in row_seats[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["row"], s["column"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


# ── USER & BOOKING QUERIES ────────────────────────────────────────────────────

def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    sql = """
        SELECT
            u.user_id,
            u.first_name,
            u.surname,
            u.first_name || ' ' || u.surname AS full_name,
            u.email,
            u.phone,
            u.date_of_birth,
            u.registered_at,
            u.is_active
        FROM registered_users u
        WHERE u.email = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (user_email,))
            row = cur.fetchone()
            return dict(row) if row else None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).

    Returns:
        dict with keys 'national_rail' (list) and 'metro' (list)
    """
    # 先取得 user_id
    user = query_user_profile(user_email)
    if user is None:
        return {"national_rail": [], "metro": []}

    user_id = user["user_id"]

    rail_sql = """
        SELECT
            b.booking_id,
            b.schedule_id,
            b.origin_station_id,
            b.destination_station_id,
            o.name AS origin_name,
            d.name AS destination_name,
            b.travel_date,
            b.departure_time,
            b.ticket_type,
            b.fare_class,
            b.coach,
            b.seat_id,
            b.stops_travelled,
            b.amount_usd,
            b.status,
            b.booked_at
        FROM national_rail_bookings b
        JOIN national_rail_stations o ON o.station_id = b.origin_station_id
        JOIN national_rail_stations d ON d.station_id = b.destination_station_id
        WHERE b.user_id = %s
        ORDER BY b.travel_date DESC, b.booked_at DESC
    """

    metro_sql = """
        SELECT
            t.trip_id,
            t.schedule_id,
            t.origin_station_id,
            t.destination_station_id,
            o.name AS origin_name,
            d.name AS destination_name,
            t.travel_date,
            t.ticket_type,
            t.stops_travelled,
            t.amount_usd,
            t.status,
            t.travelled_at
        FROM metro_travel_history t
        JOIN metro_stations o ON o.station_id = t.origin_station_id
        JOIN metro_stations d ON d.station_id = t.destination_station_id
        WHERE t.user_id = %s
        ORDER BY t.travel_date DESC, t.travelled_at DESC
    """

    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(rail_sql, (user_id,))
            national_rail = [dict(row) for row in cur.fetchall()]

            cur.execute(metro_sql, (user_id,))
            metro = [dict(row) for row in cur.fetchall()]

    return {"national_rail": national_rail, "metro": metro}


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    sql = """
        SELECT
            payment_id,
            national_rail_booking_id,
            metro_trip_id,
            amount_usd,
            method,
            status,
            paid_at
        FROM payments
        WHERE national_rail_booking_id = %s
           OR metro_trip_id = %s
        ORDER BY paid_at DESC
        LIMIT 1
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (booking_id, booking_id))
            row = cur.fetchone()
            return dict(row) if row else None


# ── TRANSACTIONAL OPERATIONS ──────────────────────────────────────────────────

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
    conn = psycopg2.connect(PG_DSN)
    try:
        conn.autocommit = False

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # 1. 確認班次存在並取得票價資訊
            cur.execute(
                """
                SELECT schedule_id, stops_in_order, first_train_time,
                       standard_base_fare_usd, standard_per_stop_rate_usd,
                       first_base_fare_usd, first_per_stop_rate_usd
                FROM national_rail_schedules
                WHERE schedule_id = %s
                """,
                (schedule_id,),
            )
            schedule = cur.fetchone()
            if schedule is None:
                return False, f"Schedule {schedule_id} not found."

            schedule = dict(schedule)
            stops = schedule["stops_in_order"]

            # 2. 確認站點順序
            if origin_station_id not in stops or destination_station_id not in stops:
                return False, "Origin or destination station not on this schedule."
            origin_pos = stops.index(origin_station_id)
            dest_pos = stops.index(destination_station_id)
            if origin_pos >= dest_pos:
                return False, "Origin must come before destination on this schedule."
            stops_travelled = dest_pos - origin_pos

            # 3. 計算票價
            if fare_class == "first":
                base = float(schedule["first_base_fare_usd"])
                per_stop = float(schedule["first_per_stop_rate_usd"])
            else:
                base = float(schedule["standard_base_fare_usd"])
                per_stop = float(schedule["standard_per_stop_rate_usd"])
            amount = round(base + per_stop * stops_travelled, 2)

            # 4. 處理 seat_id = "any"：自動選位
            if seat_id.lower() == "any":
                available = query_available_seats(schedule_id, travel_date, fare_class)
                if not available:
                    return False, "No available seats for this journey."
                seat_id = auto_select_adjacent_seats(available, 1)[0]

            # 5. 確認座位存在且屬於正確艙等
            cur.execute(
                """
                SELECT seat_id, coach, fare_class
                FROM national_rail_seats
                WHERE schedule_id = %s AND seat_id = %s
                """,
                (schedule_id, seat_id),
            )
            seat_row = cur.fetchone()
            if seat_row is None:
                return False, f"Seat {seat_id} not found on schedule {schedule_id}."
            seat_row = dict(seat_row)
            if seat_row["fare_class"] != fare_class:
                return False, f"Seat {seat_id} is {seat_row['fare_class']} class, not {fare_class}."

            # 6. 確認座位當天未被訂走（防 race condition）
            cur.execute(
                """
                SELECT 1 FROM national_rail_bookings
                WHERE schedule_id = %s
                  AND seat_id = %s
                  AND travel_date = %s::DATE
                  AND status = 'confirmed'
                FOR UPDATE
                """,
                (schedule_id, seat_id, travel_date),
            )
            if cur.fetchone():
                return False, f"Seat {seat_id} is already booked for {travel_date}."

            # 7. 取得出發時間
            departure_time = schedule["first_train_time"]

            # 8. 寫入訂票
            booking_id = _gen_booking_id()
            now = datetime.now(timezone.utc)
            cur.execute(
                """
                INSERT INTO national_rail_bookings (
                    booking_id, user_id, schedule_id,
                    origin_station_id, destination_station_id,
                    travel_date, departure_time, ticket_type, fare_class,
                    coach, seat_id, stops_travelled, amount_usd,
                    status, booked_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', %s)
                RETURNING *
                """,
                (
                    booking_id, user_id, schedule_id,
                    origin_station_id, destination_station_id,
                    travel_date, departure_time, ticket_type, fare_class,
                    seat_row["coach"], seat_id, stops_travelled, amount,
                    now,
                ),
            )
            booking = dict(cur.fetchone())

            # 9. 寫入付款記錄
            payment_id = _gen_payment_id()
            cur.execute(
                """
                INSERT INTO payments (
                    payment_id, national_rail_booking_id,
                    amount_usd, method, status, paid_at
                ) VALUES (%s, %s, %s, 'credit_card', 'paid', %s)
                """,
                (payment_id, booking_id, amount, now),
            )

        conn.commit()
        booking["payment_id"] = payment_id
        return True, booking

    except Exception as e:
        conn.rollback()
        return False, str(e)
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
    conn = psycopg2.connect(PG_DSN)
    try:
        conn.autocommit = False

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # 1. 取得訂票並鎖定
            cur.execute(
                """
                SELECT b.booking_id, b.user_id, b.amount_usd, b.status,
                       b.travel_date, s.service_type
                FROM national_rail_bookings b
                JOIN national_rail_schedules s ON s.schedule_id = b.schedule_id
                WHERE b.booking_id = %s
                FOR UPDATE
                """,
                (booking_id,),
            )
            booking = cur.fetchone()
            if booking is None:
                return False, f"Booking {booking_id} not found."
            booking = dict(booking)

            if booking["user_id"] != user_id:
                return False, "You are not authorised to cancel this booking."
            if booking["status"] != "confirmed":
                return False, f"Booking is already '{booking['status']}' and cannot be cancelled."

            # 2. 計算退款比例
            now = datetime.now(timezone.utc)
            travel_dt = datetime.combine(booking["travel_date"], datetime.min.time()).replace(tzinfo=timezone.utc)
            hours_until = (travel_dt - now).total_seconds() / 3600
            amount = float(booking["amount_usd"])
            service_type = booking["service_type"]

            if service_type == "express":
                # RF002: 100% if >48h, 50% if 24-48h, 0% if <24h
                if hours_until > 48:
                    refund_pct, policy = 1.0, "RF002: >48h — full refund"
                elif hours_until > 24:
                    refund_pct, policy = 0.5, "RF002: 24-48h — 50% refund"
                else:
                    refund_pct, policy = 0.0, "RF002: <24h — no refund"
            else:
                # RF001 (normal): 100% if >72h, 75% if 48-72h, 50% if 24-48h, 0% if <24h
                if hours_until > 72:
                    refund_pct, policy = 1.0, "RF001: >72h — full refund"
                elif hours_until > 48:
                    refund_pct, policy = 0.75, "RF001: 48-72h — 75% refund"
                elif hours_until > 24:
                    refund_pct, policy = 0.5, "RF001: 24-48h — 50% refund"
                else:
                    refund_pct, policy = 0.0, "RF001: <24h — no refund"

            refund_amount = round(amount * refund_pct, 2)

            # 3. 更新訂票狀態
            cur.execute(
                """
                UPDATE national_rail_bookings
                SET status = 'cancelled'
                WHERE booking_id = %s
                """,
                (booking_id,),
            )

            # 4. 更新付款狀態
            if refund_amount > 0:
                cur.execute(
                    """
                    UPDATE payments
                    SET status = 'refunded'
                    WHERE national_rail_booking_id = %s
                    """,
                    (booking_id,),
                )

        conn.commit()
        return True, {
            "booking_id": booking_id,
            "original_amount_usd": amount,
            "refund_amount_usd": refund_amount,
            "refund_percentage": int(refund_pct * 100),
            "policy": policy,
            "cancelled_at": now.isoformat(),
        }

    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


# ── AUTHENTICATION QUERIES ────────────────────────────────────────────────────

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
    conn = psycopg2.connect(PG_DSN)
    try:
        conn.autocommit = False

        with conn.cursor() as cur:
            # 檢查 email 是否已存在
            cur.execute(
                "SELECT 1 FROM registered_users WHERE email = %s",
                (email,),
            )
            if cur.fetchone():
                return False, f"Email '{email}' is already registered."

            # 生成 user_id
            cur.execute("SELECT COUNT(*) FROM registered_users")
            count = cur.fetchone()[0]
            user_id = f"RU{count + 1:03d}"

            now = datetime.now(timezone.utc)
            # year_of_birth → date of birth (YYYY-01-01)
            dob = f"{year_of_birth}-01-01"

            # 寫入用戶基本資料
            cur.execute(
                """
                INSERT INTO registered_users
                    (user_id, first_name, surname, email, date_of_birth, registered_at, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                """,
                (user_id, first_name, surname, email, dob, now),
            )

            # 寫入密碼（教學用：明文儲存）
            cur.execute(
                """
                INSERT INTO user_credentials
                    (user_id, password_hash, secret_question, secret_answer_hash)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, password, secret_question, secret_answer.lower()),
            )

        conn.commit()
        return True, user_id

    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials. Returns a user dict on success or None on failure.
    Dict keys: user_id, email, full_name, first_name, surname, phone, date_of_birth, is_active.
    """
    sql = """
        SELECT
            u.user_id,
            u.email,
            u.first_name || ' ' || u.surname AS full_name,
            u.first_name,
            u.surname,
            u.phone,
            u.date_of_birth,
            u.is_active
        FROM registered_users u
        JOIN user_credentials c ON c.user_id = u.user_id
        WHERE u.email = %s
          AND c.password_hash = %s
          AND u.is_active = TRUE
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (email, password))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email, or None if not found."""
    sql = """
        SELECT c.secret_question
        FROM user_credentials c
        JOIN registered_users u ON u.user_id = c.user_id
        WHERE u.email = %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
            return row[0] if row else None


def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored secret answer (case-insensitive)."""
    sql = """
        SELECT c.secret_answer_hash
        FROM user_credentials c
        JOIN registered_users u ON u.user_id = c.user_id
        WHERE u.email = %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
            if row is None:
                return False
            return row[0].lower() == answer.lower()


def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user. Returns True if the row was updated."""
    sql = """
        UPDATE user_credentials
        SET password_hash = %s,
            last_password_change = NOW()
        WHERE user_id = (
            SELECT user_id FROM registered_users WHERE email = %s
        )
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (new_password, email))
            return cur.rowcount > 0


# ── VECTOR / RAG QUERIES — do not modify ─────────────────────────────────────

def query_policy_vector_search(embedding: list[float], top_k: int = VECTOR_TOP_K) -> list[dict]:
    """
    Find the most relevant policy documents for a given query embedding.

    Args:
        embedding: Query vector from llm.embed(user_question)
        top_k:     Number of results to return

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
            return cur.fetchone()[0]