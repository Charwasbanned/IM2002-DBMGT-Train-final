"""
TransitFlow — Neo4j Seeder
Run once after starting Docker:
    python skeleton/seed_neo4j.py

Loads station and network data from train-mock-data/:
  - metro_stations.json         — city metro stations and adjacencies
  - national_rail_stations.json — national rail stations and adjacencies

Design your graph schema (node labels, relationship types, properties)
based on the data in these files, then implement the seed() function below.
"""

import json
import os
import sys

sys.path.insert(0, ".")

from neo4j import GraphDatabase
from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train-mock-data")
)


def _load(filename):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def seed():
    metro_stations = _load("metro_stations.json")
    rail_stations  = _load("national_rail_stations.json")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:

        session.run("MATCH (n) DETACH DELETE n")
        print("  Cleared existing graph data")

        # Uniqueness constraints (idempotent — IF NOT EXISTS requires Neo4j 4.1.3+)
        session.run("""
            CREATE CONSTRAINT metro_station_unique IF NOT EXISTS
            FOR (s:MetroStation) REQUIRE s.station_id IS UNIQUE
        """)
        session.run("""
            CREATE CONSTRAINT rail_station_unique IF NOT EXISTS
            FOR (s:NationalRailStation) REQUIRE s.station_id IS UNIQUE
        """)

        # Task 1: MetroStation nodes
        for station in metro_stations:
            session.run(
                """
                MERGE (s:MetroStation {station_id: $station_id})
                SET s.name                         = $name,
                    s.lines                        = $lines,
                    s.is_interchange_metro         = $is_interchange_metro,
                    s.interchange_metro_lines      = $interchange_metro_lines,
                    s.is_interchange_national_rail = $is_interchange_national_rail
                """,
                station_id=station["station_id"],
                name=station["name"],
                lines=station["lines"],
                is_interchange_metro=station["is_interchange_metro"],
                interchange_metro_lines=station["interchange_metro_lines"],
                is_interchange_national_rail=station["is_interchange_national_rail"],
            )
        print(f"  Created {len(metro_stations)} MetroStation nodes")

        # Task 2: NationalRailStation nodes
        for station in rail_stations:
            session.run(
                """
                MERGE (s:NationalRailStation {station_id: $station_id})
                SET s.name                              = $name,
                    s.lines                             = $lines,
                    s.is_interchange_national_rail      = $is_interchange_national_rail,
                    s.interchange_national_rail_lines   = $interchange_national_rail_lines,
                    s.is_interchange_metro              = $is_interchange_metro
                """,
                station_id=station["station_id"],
                name=station["name"],
                lines=station["lines"],
                is_interchange_national_rail=station["is_interchange_national_rail"],
                interchange_national_rail_lines=station["interchange_national_rail_lines"],
                is_interchange_metro=station["is_interchange_metro"],
            )
        print(f"  Created {len(rail_stations)} NationalRailStation nodes")

        # Task 3: METRO_LINK relationships (directed, one per adjacent_stations entry)
        metro_link_count = 0
        for station in metro_stations:
            for adj in station["adjacent_stations"]:
                session.run(
                    """
                    MATCH (a:MetroStation {station_id: $from_id})
                    MATCH (b:MetroStation {station_id: $to_id})
                    MERGE (a)-[:METRO_LINK {line: $line, travel_time_min: $travel_time_min}]->(b)
                    """,
                    from_id=station["station_id"],
                    to_id=adj["station_id"],
                    line=adj["line"],
                    travel_time_min=adj["travel_time_min"],
                )
                metro_link_count += 1
        print(f"  Created {metro_link_count} METRO_LINK relationships")

        # Task 4: RAIL_LINK relationships
        rail_link_count = 0
        for station in rail_stations:
            for adj in station["adjacent_stations"]:
                session.run(
                    """
                    MATCH (a:NationalRailStation {station_id: $from_id})
                    MATCH (b:NationalRailStation {station_id: $to_id})
                    MERGE (a)-[:RAIL_LINK {line: $line, travel_time_min: $travel_time_min}]->(b)
                    """,
                    from_id=station["station_id"],
                    to_id=adj["station_id"],
                    line=adj["line"],
                    travel_time_min=adj["travel_time_min"],
                )
                rail_link_count += 1
        print(f"  Created {rail_link_count} RAIL_LINK relationships")

        # Task 5: INTERCHANGE_TO relationships — both directions, no properties
        # APOC dijkstra uses defaultCost=5 for these edges at query time.
        interchange_count = 0
        for station in metro_stations:
            if station["is_interchange_national_rail"] and station["interchange_national_rail_station_id"]:
                session.run(
                    """
                    MATCH (m:MetroStation {station_id: $metro_id})
                    MATCH (nr:NationalRailStation {station_id: $rail_id})
                    MERGE (m)-[:INTERCHANGE_TO]->(nr)
                    MERGE (nr)-[:INTERCHANGE_TO]->(m)
                    """,
                    metro_id=station["station_id"],
                    rail_id=station["interchange_national_rail_station_id"],
                )
                interchange_count += 2
        print(f"  Created {interchange_count} INTERCHANGE_TO relationships")

    driver.close()
    print("\nNeo4j graph seeded successfully.")
    print("   Open http://localhost:7475 to explore the graph.")


if __name__ == "__main__":
    print("Connecting to Neo4j...")
    seed()
