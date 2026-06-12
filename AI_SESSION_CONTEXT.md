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

-- Add foreign key constraints for interchange stations.
-- DEFERRABLE INITIALLY DEFERRED: these two tables reference each other (circular FK),
-- so constraints are checked at commit time rather than per-statement.
ALTER TABLE metro_stations
    ADD CONSTRAINT fk_metro_interchange_rail
    FOREIGN KEY (interchange_national_rail_station_id)
    REFERENCES national_rail_stations(station_id)
    ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE national_rail_stations
    ADD CONSTRAINT fk_rail_interchange_metro
    FOREIGN KEY (interchange_metro_station_id)
    REFERENCES metro_stations(station_id)
    ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

-- Metro Schedules (8 schedules for 4 lines)
CREATE TABLE metro_schedules (
    schedule_id VARCHAR(20) PRIMARY KEY,
    line VARCHAR(10) NOT NULL,
    direction VARCHAR(20) NOT NULL,
    origin_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
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
    origin_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
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
    CHECK (standard_per_stop_rate_usd >= 0),
    CHECK (first_base_fare_usd >= 0),
    CHECK (first_per_stop_rate_usd >= 0),
    CHECK (frequency_min > 0)
);

-- National Rail Seats (Flattened structure - 1 table)
CREATE TABLE national_rail_seats (
    schedule_id VARCHAR(20) NOT NULL REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    seat_id VARCHAR(10) NOT NULL,
    coach VARCHAR(5) NOT NULL,
    fare_class VARCHAR(20) NOT NULL,
    seat_row INTEGER NOT NULL,
    seat_column VARCHAR(2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (schedule_id, seat_id),
    CHECK (seat_row > 0),
    CHECK (fare_class IN ('standard', 'first'))
);

-- ============================================================
--  PART 2: USER TABLES
-- ============================================================

-- Registered Users (Basic Information)
CREATE TABLE registered_users (
    user_id VARCHAR(10) PRIMARY KEY,
    full_name VARCHAR(200) NOT NULL,
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
    user_id VARCHAR(10) NOT NULL REFERENCES registered_users(user_id) ON DELETE RESTRICT,
    schedule_id VARCHAR(20) NOT NULL REFERENCES national_rail_schedules(schedule_id) ON DELETE RESTRICT,
    origin_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) NOT NULL REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
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
    user_id VARCHAR(10) NOT NULL REFERENCES registered_users(user_id) ON DELETE RESTRICT,
    schedule_id VARCHAR(20) NOT NULL REFERENCES metro_schedules(schedule_id) ON DELETE RESTRICT,
    origin_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    destination_station_id VARCHAR(10) NOT NULL REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
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
    CHECK (status IN ('completed', 'cancelled')),
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
CREATE INDEX idx_users_active ON registered_users(is_active);

-- National Rail Booking Indexes
CREATE INDEX idx_bookings_user ON national_rail_bookings(user_id);
CREATE INDEX idx_bookings_travel_date ON national_rail_bookings(travel_date);
CREATE INDEX idx_bookings_status ON national_rail_bookings(status);
CREATE INDEX idx_bookings_schedule ON national_rail_bookings(schedule_id);
CREATE INDEX idx_bookings_user_date ON national_rail_bookings(user_id, travel_date);
CREATE INDEX idx_bookings_origin ON national_rail_bookings(origin_station_id);
CREATE INDEX idx_bookings_destination ON national_rail_bookings(destination_station_id);

-- Metro Travel Indexes
CREATE INDEX idx_metro_travel_user ON metro_travel_history(user_id);
CREATE INDEX idx_metro_travel_date ON metro_travel_history(travel_date);
CREATE INDEX idx_metro_travel_schedule ON metro_travel_history(schedule_id);
CREATE INDEX idx_metro_travel_origin ON metro_travel_history(origin_station_id);
CREATE INDEX idx_metro_travel_destination ON metro_travel_history(destination_station_id);

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
CREATE INDEX idx_metro_schedules_origin ON metro_schedules(origin_station_id);
CREATE INDEX idx_metro_schedules_destination ON metro_schedules(destination_station_id);

CREATE INDEX idx_rail_schedules_line ON national_rail_schedules(line);
CREATE INDEX idx_rail_schedules_origin ON national_rail_schedules(origin_station_id);
CREATE INDEX idx_rail_schedules_destination ON national_rail_schedules(destination_station_id);
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
COMMENT ON COLUMN registered_users.full_name IS 'Full display name (matches full_name field in registered_users.json mock data)';
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
- MetroStation       — 20 nodes, one per city metro station
- NationalRailStation — 10 nodes, one per national rail station

Node properties (MetroStation):
  station_id                   : string  — e.g. "MS01" (unique, matches relational schema)
  name                         : string  — e.g. "Central Square"
  lines                        : list    — e.g. ["M1", "M2"]
  is_interchange_metro         : boolean
  interchange_metro_lines      : list    — metro lines that interchange here
  is_interchange_national_rail : boolean

Node properties (NationalRailStation):
  station_id                       : string  — e.g. "NR01" (unique)
  name                             : string  — e.g. "Central Station"
  lines                            : list    — e.g. ["NR1", "NR2"]
  is_interchange_national_rail     : boolean
  interchange_national_rail_lines  : list
  is_interchange_metro             : boolean

Relationship types:
- METRO_LINK       MetroStation → MetroStation
                   properties: line (string), travel_time_min (int)
                   Direction: one directed edge per adjacent_stations entry
                   (both A→B and B→A edges exist — network is fully traversable)

- RAIL_LINK        NationalRailStation → NationalRailStation
                   properties: line (string), travel_time_min (int)
                   Same direction rule as METRO_LINK

- INTERCHANGE_TO   MetroStation ↔ NationalRailStation (both directions stored)
                   properties: NONE — the existence of the edge is the fact
                   Cross-network transfer points:
                     MS01 (Central Square)  ↔  NR01 (Central Station)
                     MS07 (Old Town)        ↔  NR03 (Old Town Junction)
                     MS15 (Ferndale)        ↔  NR07 (Ferndale Halt)

Constraints:
  CREATE CONSTRAINT metro_station_unique IF NOT EXISTS
    FOR (s:MetroStation) REQUIRE s.station_id IS UNIQUE;
  CREATE CONSTRAINT rail_station_unique IF NOT EXISTS
    FOR (s:NationalRailStation) REQUIRE s.station_id IS UNIQUE;

Path-finding notes for query implementation:
- Use travel_time_min on METRO_LINK / RAIL_LINK as APOC dijkstra cost property.
  INTERCHANGE_TO has no properties — pass defaultCost=5 in the APOC call:
    apoc.algo.dijkstra(a, b, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', 'travel_time_min', 5)
- Variable-length traversal pattern for delay ripple (embed hops as f-string literal):
    -[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..{hops}]-
- "network" parameter ("metro" | "rail" | "auto") maps to which rel types
  to include in the MATCH pattern. Infer "auto" from ID prefix: MS* = metro, NR* = rail.
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

### **Schema Design Decisions**

- [ ] **User Name Fields**: Single `full_name VARCHAR(200)` column. Why: Matches `full_name` field in `registered_users.json` mock data and `agent.py` usage of `profile['full_name']`. The `register_user()` function still accepts `first_name` and `surname` separately and concatenates them on insert.

- [ ] **Primary Key Strategy**: Mixed approach — Natural keys for infrastructure (MS01, NR_SCH01), App-generated for transactions (BK-XXXXXX, MT-XXXXXX), SERIAL for internal tables. Why: Natural keys are human-readable and stable; app-generated IDs prevent information leakage; SERIAL simplifies internal references.

- [ ] **Seat Layout Normalization**: Flattened to single `national_rail_seats` table instead of 3-table normalized structure (layouts → coaches → seats). Why: Read-heavy workload; eliminates 2 JOINs for seat availability queries; seat configurations are essentially static after seeding (per Tutorial Section 3.3).

- [ ] **Schedule Stop Ordering**: Using PostgreSQL `TEXT[]` arrays for `stops_in_order` + `JSONB` for `travel_time_from_origin_min`. Why: Preserves stop sequence without junction table; enables single-table queries; GIN indexes support efficient containment checks.

- [ ] **Polymorphic Association (Payments/Feedback)**: Separate FK columns (`national_rail_booking_id`, `metro_trip_id`) with CHECK constraint for mutual exclusivity. Why: Maintains full referential integrity at database level; prevents orphaned records; clearer than string-prefix discriminator approach.

- [ ] **Soft Delete Pattern**: `is_active` boolean on `registered_users` instead of physical deletion. Why: Preserves audit trail; enables account recovery; complies with financial record retention requirements.

- [ ] **Interchange Station Modeling**: Bidirectional nullable FKs between `metro_stations` and `national_rail_stations`. Why: Represents real-world cross-network transfer points; supports both metro-to-rail and rail-to-metro lookups.

- [ ] **Self-Referencing FK**: `metro_travel_history.day_pass_ref` references same table with `ON DELETE SET NULL`. Why: Links subsequent day-pass uses to original purchase; preserves trip history even if original pass record is removed.

- [ ] **Timestamp Strategy**: `TIMESTAMP WITH TIME ZONE` for all event times (`booked_at`, `travelled_at`, `paid_at`); `DATE` for calendar dates (`travel_date`, `date_of_birth`); `TIME` for schedule times. Why: Timezone-aware for distributed systems; appropriate precision for each use case.

- [ ] **Money Type**: `NUMERIC(10,2)` for all currency fields instead of `FLOAT`. Why: Exact arithmetic; prevents rounding errors in financial calculations.

- [ ] **Constraint Strategy**: CHECK constraints for enums (`status IN (...)`) instead of native PostgreSQL ENUMs. Why: Easier to modify; no type system complexity; sufficient for stable value sets.

### **Index Design Decisions**

- [ ] **Foreign Key Indexes**: Created indexes on all FK columns (`user_id`, `schedule_id`, `origin_station_id`, etc.). Why: PostgreSQL doesn't auto-index FKs; critical for JOIN performance and ON DELETE enforcement.

- [ ] **Composite Indexes**: `idx_bookings_user_date` on `(user_id, travel_date)`. Why: Common query pattern for user booking history filtered by date range.

- [ ] **Polymorphic FK Indexes**: Both `national_rail_booking_id` and `metro_trip_id` indexed in `payments` and `feedback`. Why: Supports lookups from either direction despite mutual exclusivity.

- [ ] **GIN Indexes**: Applied to `TEXT[]` and `JSONB` columns (`stops_in_order`, `operates_on`). Why: Enables efficient array containment queries (`@>` operator) for schedule filtering.

- [ ] **Vector Index**: HNSW on `policy_documents.embedding` with `vector_cosine_ops`. Why: Approximate nearest-neighbor search for RAG; balances speed vs accuracy (Tutorial Section 6.4).

### **Graph Schema Decisions**

- [ ] **Node Labels**: Two separate labels — `MetroStation` and `NationalRailStation` — instead of a single `Station` with a `network` property. Why: Cleaner label-based filtering in Cypher; type-specific constraints and indexes; matches tutorial section 14.2 design.

- [ ] **Relationship Types**: Three separate types — `METRO_LINK`, `RAIL_LINK`, `INTERCHANGE_TO` — instead of a single `CONNECTS_TO` with a type property. Why: Allows path queries to filter by rel type (e.g. metro-only vs cross-network); each type has its own property structure; consistent with tutorial section 14.2.

- [ ] **Relationship Direction**: Directed edges, one per `adjacent_stations` entry. Since each station lists its neighbours and the listing is symmetric, both A→B and B→A edges exist — network is fully traversable in both directions without undirected match syntax.

- [ ] **INTERCHANGE_TO has no properties**: The existence of the edge is the fact. Transfer time of 5 min is handled at query time via APOC `defaultCost=5` parameter, not stored on the edge.

- [ ] **INTERCHANGE_TO is bidirectional**: Both MetroStation→NationalRailStation and NationalRailStation→MetroStation directed edges are created. Why: Allows pathfinding to cross the network boundary in either direction without undirected Cypher syntax.

- [ ] **Cost property for APOC Dijkstra**: `travel_time_min` on `METRO_LINK` and `RAIL_LINK`. Cross-network paths use `apoc.algo.dijkstra(a, b, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', 'travel_time_min', 5)` with `defaultCost=5` covering `INTERCHANGE_TO` edges.

- [ ] **`network="auto"` inference**: Infer from station ID prefix — `MS*` → metro (`METRO_LINK`), `NR*` → rail (`RAIL_LINK`). Why: Simple and reliable without adding a redundant `network` property to nodes; agent.py already intercepts mixed MS/NR pairs and routes them to `query_interchange_path`.

- [ ] **`query_cheapest_route` strategy**: Use Neo4j to find the shortest path by hop count (fewest stops), then query PostgreSQL `metro_schedules` / `national_rail_schedules` for the fare rates, then calculate estimated total fare. Why: Graph DB handles topology; relational DB holds fare rates — each DB does what it does best. Fare is explicitly labelled "approximate" in the return dict.

- [ ] **`query_station_connections` return format**: `{station_id, name, line, travel_time_min}` per neighbour. `line` is `null` for `INTERCHANGE_TO` connections. Why: Sufficient for agent use; network type is distinguishable from the station ID prefix.

- [] **Uniqueness constraints**: `CREATE CONSTRAINT ... FOR (s:MetroStation) REQUIRE s.station_id IS UNIQUE` and equivalent for `NationalRailStation`. Applied at seeder startup with `IF NOT EXISTS`. Why: Prevents duplicate nodes if seeder is accidentally run without clearing the graph first; MERGE operations rely on this to be idempotent.

### **Data Type Decisions**

- [ ] **Station IDs**: `VARCHAR(10)` instead of `CHAR(4)`. Why: Variable-length prefix codes (MS01, NR01); VARCHAR has identical performance to CHAR in PostgreSQL.

- [ ] **Booking/Payment IDs**: `VARCHAR(20)` for app-generated IDs. Why: Accommodates prefix + 6-char random suffix (BK-XXXXXX) with room for future expansion.

- [ ] **Password Hash**: `VARCHAR(255)` instead of `VARCHAR(60)`. Why: argon2id output (~97 chars) is longer than bcrypt (60 chars); future-proofs for algorithm changes.

- [ ] **Arrays vs JSONB**: `TEXT[]` for ordered lists (`stops_in_order`), `JSONB` for key-value maps (`travel_time_from_origin_min`). Why: Arrays preserve order and support GIN indexing; JSONB enables key-based lookups.


## Prompts That Worked

<!-- Share prompts that produced good output so teammates can reuse them. -->

### Schema design prompt that worked:
```

```

### Query implementation prompt that worked:
```

```
