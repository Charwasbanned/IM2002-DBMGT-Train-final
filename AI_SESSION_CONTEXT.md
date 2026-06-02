# AI Session Context — TransitFlow

**How to use this file:**
At the start of every AI coding session, paste the full contents of this file as your first message to your AI assistant. This gives the AI the context it needs to produce code that fits your codebase and is consistent with your teammates' work.

**Who maintains this file:**
Whoever makes a schema change or architectural decision updates this file in the same commit. Treat it like a team contract.

---

## Project Overview

TransitFlow is a Python-based AI chat assistant for a fictional transit operator. It queries three databases — PostgreSQL (relational + vector), Neo4j (graph) — and uses an LLM to answer user questions. Our task as students is to design the database schema and implement the query functions in `databases/relational/queries.py` and `databases/graph/queries.py`.

## Tech Stack

- Language: Python 3.11+
- Relational DB: PostgreSQL via `psycopg2` with `RealDictCursor`
- Graph DB: Neo4j via the `neo4j` Python driver
- Vector search: `pgvector` extension (already implemented — do not modify)
- Web UI: Gradio
- LLM: Google Gemini or local Ollama (configured via `.env`)

## Coding Conventions

- **Naming:** `snake_case` for all Python names and SQL identifiers
- **Docstrings:** All functions must have a docstring with `Args:` and `Returns:` sections
- **Return types:** Use type hints. Read-only functions return `list[dict]` or `Optional[dict]`
- **Empty results:** Return `[]` or `None` (as documented), never raise an exception for "not found"
- **SQL:** Use `%s` placeholders for all user inputs — never string-format into SQL
- **Relational pattern:** Use `_connect()` helper + `psycopg2.extras.RealDictCursor`:
  ```python
  with _connect() as conn:
      with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
          cur.execute("SELECT ...", (param,))
          return [dict(row) for row in cur.fetchall()]
  ```
- **Graph pattern:** Use `_driver()` helper + session:
  ```python
  with _driver() as driver:
      with driver.session() as session:
          result = session.run("MATCH ...", station_id=station_id)
          return [dict(record) for record in result]
  ```

## Agreed Relational Schema

<!-- ============================================================
  FILL THIS IN after your team completes the schema design workshop.
  Paste your final CREATE TABLE statements here.
  ============================================================ -->

```sql
-- ============================================================
--  PART 1: INFRASTRUCTURE TABLES
-- ============================================================

-- Metro Stations (20 stations across 4 lines)
CREATE TABLE metro_stations (
    station_id VARCHAR(10) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    lines TEXT[] NOT NULL,
    is_interchange_metro BOOLEAN DEFAULT FALSE,
    is_interchange_national_rail BOOLEAN DEFAULT FALSE,
    interchange_national_rail_station_id VARCHAR(10),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- National Rail Stations (10 stations across 2 lines)
CREATE TABLE national_rail_stations (
    station_id VARCHAR(10) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    lines TEXT[] NOT NULL,
    is_interchange_national_rail BOOLEAN DEFAULT FALSE,
    is_interchange_metro BOOLEAN DEFAULT FALSE,
    interchange_metro_station_id VARCHAR(10),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Add foreign key constraints for interchange stations
ALTER TABLE metro_stations
    ADD CONSTRAINT fk_metro_interchange_rail
    FOREIGN KEY (interchange_national_rail_station_id)
    REFERENCES national_rail_stations(station_id);

ALTER TABLE national_rail_stations
    ADD CONSTRAINT fk_rail_interchange_metro
    FOREIGN KEY (interchange_metro_station_id)
    REFERENCES metro_stations(station_id);

-- Metro Schedules (8 schedules for 4 lines)
CREATE TABLE metro_schedules (
    schedule_id VARCHAR(20) PRIMARY KEY,
    line VARCHAR(10) NOT NULL,
    direction VARCHAR(20) NOT NULL,
    origin_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id),
    destination_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id),
    stops_in_order TEXT[] NOT NULL,
    first_train_time TIME NOT NULL,
    last_train_time TIME NOT NULL,
    travel_time_from_origin_min JSONB NOT NULL,
    base_fare_usd NUMERIC(10,2) NOT NULL,
    per_stop_rate_usd NUMERIC(10,2) NOT NULL,
    frequency_min INTEGER NOT NULL,
    operates_on TEXT[] NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (base_fare_usd >= 0),
    CHECK (per_stop_rate_usd >= 0),
    CHECK (frequency_min > 0)
);

-- National Rail Schedules (8 schedules: 4 normal + 4 express)
CREATE TABLE national_rail_schedules (
    schedule_id VARCHAR(20) PRIMARY KEY,
    line VARCHAR(10) NOT NULL,
    service_type VARCHAR(20) NOT NULL,
    direction VARCHAR(20) NOT NULL,
    origin_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id),
    destination_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id),
    stops_in_order TEXT[] NOT NULL,
    passed_through_stations TEXT[],
    first_train_time TIME NOT NULL,
    last_train_time TIME NOT NULL,
    travel_time_from_origin_min JSONB NOT NULL,
    standard_base_fare_usd NUMERIC(10,2) NOT NULL,
    standard_per_stop_rate_usd NUMERIC(10,2) NOT NULL,
    first_base_fare_usd NUMERIC(10,2) NOT NULL,
    first_per_stop_rate_usd NUMERIC(10,2) NOT NULL,
    frequency_min INTEGER NOT NULL,
    operates_on TEXT[] NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (standard_base_fare_usd >= 0),
    CHECK (first_base_fare_usd >= 0),
    CHECK (frequency_min > 0)
);

-- National Rail Seats (Flattened structure - 1 table)
CREATE TABLE national_rail_seats (
    schedule_id VARCHAR(20) NOT NULL REFERENCES national_rail_schedules(schedule_id),
    seat_id VARCHAR(10) NOT NULL,
    coach VARCHAR(5) NOT NULL,
    fare_class VARCHAR(20) NOT NULL,
    seat_row INTEGER NOT NULL,
    seat_column VARCHAR(2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (schedule_id, seat_id),
    CHECK (seat_row > 0)
);

-- ============================================================
--  PART 2: USER TABLES
-- ============================================================

-- Registered Users (Basic Information)
CREATE TABLE registered_users (
    user_id VARCHAR(10) PRIMARY KEY,
    first_name VARCHAR(100) NOT NULL,
    surname VARCHAR(100) NOT NULL,
    email VARCHAR(100) NOT NULL UNIQUE,
    phone VARCHAR(20),
    date_of_birth DATE,
    registered_at TIMESTAMP WITH TIME ZONE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User Credentials (Separated for Security)
CREATE TABLE user_credentials (
    user_id VARCHAR(10) PRIMARY KEY REFERENCES registered_users(user_id) ON DELETE CASCADE,
    password_hash VARCHAR(255) NOT NULL,
    secret_question TEXT,
    secret_answer_hash VARCHAR(255),
    last_password_change TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
--  PART 3: TRANSACTION TABLES
-- ============================================================

-- National Rail Bookings
CREATE TABLE national_rail_bookings (
    booking_id VARCHAR(20) PRIMARY KEY,
    user_id VARCHAR(10) NOT NULL REFERENCES registered_users(user_id),
    schedule_id VARCHAR(20) NOT NULL REFERENCES national_rail_schedules(schedule_id),
    origin_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id),
    destination_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id),
    travel_date DATE NOT NULL,
    departure_time TIME NOT NULL,
    ticket_type VARCHAR(20) NOT NULL,
    fare_class VARCHAR(20) NOT NULL,
    coach VARCHAR(5) NOT NULL,
    seat_id VARCHAR(10) NOT NULL,
    stops_travelled INTEGER NOT NULL,
    amount_usd NUMERIC(10,2) NOT NULL,
    status VARCHAR(20) NOT NULL,
    booked_at TIMESTAMP WITH TIME ZONE NOT NULL,
    travelled_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount_usd >= 0),
    CHECK (stops_travelled > 0),
    CHECK (status IN ('confirmed', 'completed', 'cancelled', 'refunded')),
    CHECK (ticket_type IN ('single', 'return')),
    CHECK (fare_class IN ('standard', 'first'))
);

-- Metro Travel History
CREATE TABLE metro_travel_history (
    trip_id VARCHAR(20) PRIMARY KEY,
    user_id VARCHAR(10) NOT NULL REFERENCES registered_users(user_id),
    schedule_id VARCHAR(20) NOT NULL REFERENCES metro_schedules(schedule_id),
    origin_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id),
    destination_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id),
    travel_date DATE NOT NULL,
    ticket_type VARCHAR(20) NOT NULL,
    day_pass_ref VARCHAR(20),
    stops_travelled INTEGER,
    amount_usd NUMERIC(10,2) NOT NULL,
    status VARCHAR(20) NOT NULL,
    purchased_at TIMESTAMP WITH TIME ZONE,
    travelled_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount_usd >= 0),
    CHECK (stops_travelled IS NULL OR stops_travelled > 0),
    CHECK (status IN ('completed', 'refunded')),
    CHECK (ticket_type IN ('single', 'day_pass'))
);

-- Self-referencing FK for day pass
ALTER TABLE metro_travel_history
    ADD CONSTRAINT fk_metro_day_pass
    FOREIGN KEY (day_pass_ref)
    REFERENCES metro_travel_history(trip_id)
    ON DELETE SET NULL;

-- Payments (Separate FK Columns for Polymorphic Association)
CREATE TABLE payments (
    payment_id VARCHAR(20) PRIMARY KEY,
    national_rail_booking_id VARCHAR(20) REFERENCES national_rail_bookings(booking_id),
    metro_trip_id VARCHAR(20) REFERENCES metro_travel_history(trip_id),
    amount_usd NUMERIC(10,2) NOT NULL,
    method VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    paid_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount_usd >= 0),
    CHECK (method IN ('credit_card', 'debit_card', 'ewallet')),
    CHECK (status IN ('paid', 'refunded', 'pending')),
    CHECK (
        (national_rail_booking_id IS NOT NULL AND metro_trip_id IS NULL) OR
        (national_rail_booking_id IS NULL AND metro_trip_id IS NOT NULL)
    )
);

-- Feedback (Separate FK Columns for Polymorphic Association)
CREATE TABLE feedback (
    feedback_id VARCHAR(20) PRIMARY KEY,
    national_rail_booking_id VARCHAR(20) REFERENCES national_rail_bookings(booking_id),
    metro_trip_id VARCHAR(20) REFERENCES metro_travel_history(trip_id),
    user_id VARCHAR(10) NOT NULL REFERENCES registered_users(user_id),
    rating INTEGER NOT NULL,
    comment TEXT,
    submitted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (rating >= 1 AND rating <= 5),
    CHECK (
        (national_rail_booking_id IS NOT NULL AND metro_trip_id IS NULL) OR
        (national_rail_booking_id IS NULL AND metro_trip_id IS NOT NULL)
    )
);

-- ============================================================
--  PART 4: INDEXES FOR PERFORMANCE
-- ============================================================

-- User Indexes
CREATE INDEX idx_users_email ON registered_users(email);
CREATE INDEX idx_users_active ON registered_users(is_active);

-- National Rail Booking Indexes
CREATE INDEX idx_bookings_user ON national_rail_bookings(user_id);
CREATE INDEX idx_bookings_travel_date ON national_rail_bookings(travel_date);
CREATE INDEX idx_bookings_status ON national_rail_bookings(status);
CREATE INDEX idx_bookings_schedule ON national_rail_bookings(schedule_id);
CREATE INDEX idx_bookings_user_date ON national_rail_bookings(user_id, travel_date);
CREATE INDEX idx_bookings_origin ON national_rail_bookings(origin_id);
CREATE INDEX idx_bookings_destination ON national_rail_bookings(destination_id);

-- Metro Travel Indexes
CREATE INDEX idx_metro_travel_user ON metro_travel_history(user_id);
CREATE INDEX idx_metro_travel_date ON metro_travel_history(travel_date);
CREATE INDEX idx_metro_travel_schedule ON metro_travel_history(schedule_id);
CREATE INDEX idx_metro_travel_origin ON metro_travel_history(origin_id);
CREATE INDEX idx_metro_travel_destination ON metro_travel_history(destination_id);

-- Payment Indexes (Both FK columns)
CREATE INDEX idx_payments_booking ON payments(national_rail_booking_id);
CREATE INDEX idx_payments_trip ON payments(metro_trip_id);
CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_payments_method ON payments(method);

-- Feedback Indexes (Both FK columns)
CREATE INDEX idx_feedback_booking ON feedback(national_rail_booking_id);
CREATE INDEX idx_feedback_trip ON feedback(metro_trip_id);
CREATE INDEX idx_feedback_user ON feedback(user_id);
CREATE INDEX idx_feedback_rating ON feedback(rating);

-- Schedule Indexes
CREATE INDEX idx_metro_schedules_line ON metro_schedules(line);
CREATE INDEX idx_metro_schedules_origin ON metro_schedules(origin_id);
CREATE INDEX idx_metro_schedules_destination ON metro_schedules(destination_id);

CREATE INDEX idx_rail_schedules_line ON national_rail_schedules(line);
CREATE INDEX idx_rail_schedules_origin ON national_rail_schedules(origin_id);
CREATE INDEX idx_rail_schedules_destination ON national_rail_schedules(destination_id);
CREATE INDEX idx_rail_schedules_service_type ON national_rail_schedules(service_type);

-- Seat Indexes
CREATE INDEX idx_seats_schedule ON national_rail_seats(schedule_id);
CREATE INDEX idx_seats_fare_class ON national_rail_seats(fare_class);

-- JSONB/Array Indexes for containment queries
CREATE INDEX idx_metro_schedules_stops ON metro_schedules USING GIN (stops_in_order);
CREATE INDEX idx_metro_schedules_operates ON metro_schedules USING GIN (operates_on);
CREATE INDEX idx_rail_schedules_stops ON national_rail_schedules USING GIN (stops_in_order);
CREATE INDEX idx_rail_schedules_operates ON national_rail_schedules USING GIN (operates_on);

-- ============================================================
--  PART 5: COMMENTS FOR DOCUMENTATION
-- ============================================================

COMMENT ON TABLE metro_stations IS 'City metro network stations (20 stations across 4 lines: M1, M2, M3, M4)';
COMMENT ON TABLE national_rail_stations IS 'Intercity rail network stations (10 stations across 2 lines: NR1, NR2)';
COMMENT ON TABLE metro_schedules IS 'Metro timetables with fare structure and JSONB stop sequences';
COMMENT ON TABLE national_rail_schedules IS 'National rail timetables with normal and express services';
COMMENT ON TABLE national_rail_seats IS 'Flattened seat layout (schedule + seat + coach in one table)';
COMMENT ON TABLE registered_users IS 'User basic information (passwords stored separately in user_credentials)';
COMMENT ON TABLE user_credentials IS 'Authentication data with argon2id password hashes';
COMMENT ON TABLE national_rail_bookings IS 'Advance bookings for national rail with seat assignments';
COMMENT ON TABLE metro_travel_history IS 'Same-day metro tap-in travel records';
COMMENT ON TABLE payments IS 'Payment records with separate FK columns for bookings and trips';
COMMENT ON TABLE feedback IS 'Post-travel passenger ratings and comments with separate FK columns';

COMMENT ON COLUMN metro_schedules.stops_in_order IS 'Array of station IDs in travel order';
COMMENT ON COLUMN metro_schedules.travel_time_from_origin_min IS 'JSONB map: {station_id: minutes_from_origin}';
COMMENT ON COLUMN national_rail_schedules.passed_through_stations IS 'Stations passed but not stopped at (express services only)';
COMMENT ON COLUMN user_credentials.password_hash IS 'argon2id hash (time_cost=2, memory_cost=65536, parallelism=2)';
COMMENT ON COLUMN payments.national_rail_booking_id IS 'FK to national_rail_bookings (mutually exclusive with metro_trip_id)';
COMMENT ON COLUMN payments.metro_trip_id IS 'FK to metro_travel_history (mutually exclusive with national_rail_booking_id)';
COMMENT ON COLUMN registered_users.first_name IS 'User first name (matches register_user function signature)';
COMMENT ON COLUMN registered_users.surname IS 'User surname (matches register_user function signature)';
COMMENT ON COLUMN registered_users.date_of_birth IS 'Full date of birth (year_of_birth converted to YYYY-01-01 in Python)';

-- ============================================================
--  VECTOR SCHEMA  (RAG / Help Desk) — do not modify
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_documents (
    id          SERIAL       PRIMARY KEY,
    title       VARCHAR(200) NOT NULL,
    category    VARCHAR(50)  NOT NULL,  -- 'refund', 'booking', 'conduct'
    content     TEXT         NOT NULL,
    -- 768-dim  → Ollama nomic-embed-text (default)
    -- 3072-dim → Gemini gemini-embedding-001
    -- If you switch LLM_PROVIDER to gemini, change to vector(3072) and reset the database.
    embedding   vector(768),
    source_file VARCHAR(200),
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- Index for fast cosine similarity search
CREATE INDEX IF NOT EXISTS idx_policy_embedding ON policy_documents USING hnsw (embedding vector_cosine_ops);
```

## Agreed Graph Schema

<!-- ============================================================
  FILL THIS IN after your team agrees on Neo4j node labels and
  relationship types.
  ============================================================ -->

```
Node labels:
- TODO

Relationship types:
- TODO

Key properties:
- TODO
```

## Function Signatures We Are Implementing

These are fixed contracts. AI-generated code must match these signatures exactly.

### Relational (`databases/relational/queries.py`)

```python
# Read-only
def query_national_rail_availability(origin_id: str, destination_id: str, travel_date: Optional[str] = None) -> list[dict]: ...
def query_national_rail_fare(schedule_id: str, fare_class: str, stops_travelled: int) -> Optional[dict]: ...
def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]: ...
def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]: ...
def query_available_seats(schedule_id: str, travel_date: str, fare_class: str) -> list[dict]: ...
def query_user_profile(user_email: str) -> Optional[dict]: ...
def query_user_bookings(user_email: str) -> dict: ...  # returns {"national_rail": [...], "metro": [...]}
def query_payment_info(booking_id: str) -> Optional[dict]: ...

# Write operations
def execute_booking(user_id, schedule_id, origin_station_id, destination_station_id, travel_date, fare_class, seat_id, ticket_type="single") -> tuple[bool, dict | str]: ...
def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]: ...

# Auth
def register_user(email, first_name, surname, year_of_birth, password, secret_question, secret_answer) -> tuple[bool, str]: ...
def login_user(email: str, password: str) -> Optional[dict]: ...
def get_user_secret_question(email: str) -> Optional[str]: ...
def verify_secret_answer(email: str, answer: str) -> bool: ...
def update_password(email: str, new_password: str) -> bool: ...
```

### Graph (`databases/graph/queries.py`)

```python
def query_shortest_route(origin_id: str, destination_id: str, network: str = "auto") -> dict: ...
def query_cheapest_route(origin_id: str, destination_id: str, network: str = "auto", fare_class: str = "standard") -> dict: ...
def query_alternative_routes(origin_id, destination_id, avoid_station_id, network="auto", max_routes=3) -> list[list[dict]]: ...
def query_interchange_path(origin_id: str, destination_id: str) -> dict: ...
def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]: ...
def query_station_connections(station_id: str) -> list[dict]: ...
```

## Team Decisions Log

<!-- Add entries as you make decisions. Format: "Decision: X. Why: Y." -->

- [ ] Schema design: TODO — add your table/column decisions here
- [ ] Graph schema: TODO — add your node label and relationship type decisions here
- [ ] (example) Metro schedule stop ordering: using `jsonb_array_elements` approach — easier to debug than containment operators

## Prompts That Worked

<!-- Share prompts that produced good output so teammates can reuse them. -->

### Schema design prompt that worked:
```
TODO — add a prompt here after your schema design workshop
```

### Query implementation prompt that worked:
```
TODO — add after implementing your first function
```
