"""
TransitFlow — Neo4j Graph Database Layer
=========================================
This module handles all queries to Neo4j.

GRAPH ROLE:
  - Model the dual transit network (city metro M1–M4 + national rail NR1–NR2)
  - Find fastest routes (Dijkstra by travel_time_min via APOC)
  - Find cheapest routes (Dijkstra by fare via APOC)
  - Find alternative routes avoiding a given station
  - Find cross-network interchange paths (metro → rail or rail → metro)
  - Show delay ripple: which stations are affected within N hops

STUDENT TASK
------------
Design your graph schema (node labels, relationship types, properties)
based on the data in train-mock-data/, seed it with skeleton/seed_neo4j.py,
then implement the query_ functions below.

Functions prefixed with `query_` are called by the agent (skeleton/agent.py).
"""

from __future__ import annotations

from typing import Optional

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


def _driver():
    """Return a Neo4j driver. Caller is responsible for closing."""
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a session, run Cypher, return data.

def example_count_nodes() -> int:
    """Example: count all nodes currently in the graph."""
    with _driver() as driver:
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS total")
            return result.single()["total"]

# TODO: Implement the query_ functions below.
# ─────────────────────────────────────────────────────────────────────────────


# ── FASTEST ROUTE (Dijkstra by travel_time_min) ───────────────────────────────

def query_shortest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
) -> dict:
    """
    Find the fastest path between two stations, minimising total travel time.
    Uses apoc.algo.dijkstra (APOC required; enabled in docker-compose.yml).

    Args:
        origin_id:       e.g. "MS01" or "NR01"
        destination_id:  e.g. "MS09" or "NR05"
        network:         "metro", "rail", or "auto" (inferred from IDs)

    Returns:
        dict with keys: found, origin_id, destination_id,
                        total_time_min, path (list of station dicts), legs
    """
    if network == "auto":
        network = "metro" if origin_id.upper().startswith("MS") else "rail"

    rel_type = "METRO_LINK" if network == "metro" else "RAIL_LINK"
    label    = "MetroStation" if network == "metro" else "NationalRailStation"

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (start:{label} {{station_id: $origin}})
                MATCH (end:{label}   {{station_id: $destination}})
                CALL apoc.algo.dijkstra(start, end, '{rel_type}', 'travel_time_min')
                YIELD path, weight
                RETURN
                    [n IN nodes(path) | n.station_id]              AS station_ids,
                    [n IN nodes(path) | n.name]                    AS station_names,
                    [r IN relationships(path) | r.line]            AS lines,
                    [r IN relationships(path) | r.travel_time_min] AS segment_times,
                    weight                                         AS total_time_min
                """,
                origin=origin_id,
                destination=destination_id,
            )
            row = result.single()
            if not row:
                return {"found": False, "origin_id": origin_id, "destination_id": destination_id}

            station_ids   = list(row["station_ids"])
            station_names = list(row["station_names"])
            lines         = list(row["lines"])
            segment_times = list(row["segment_times"])

            path = [
                {"station_id": sid, "name": sname}
                for sid, sname in zip(station_ids, station_names)
            ]
            legs = [
                {
                    "from":            station_ids[i],
                    "from_name":       station_names[i],
                    "to":              station_ids[i + 1],
                    "to_name":         station_names[i + 1],
                    "line":            lines[i],
                    "travel_time_min": segment_times[i],
                }
                for i in range(len(lines))
            ]

            return {
                "found":          True,
                "origin_id":      origin_id,
                "destination_id": destination_id,
                "total_time_min": row["total_time_min"],
                "path":           path,
                "legs":           legs,
            }


# ── CHEAPEST ROUTE (Dijkstra by fare) ────────────────────────────────────────

def query_cheapest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
    fare_class: str = "standard",
) -> dict:
    """
    Find the cheapest path between two stations, minimising total estimated fare.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        network:         "metro", "rail", or "auto"
        fare_class:      "standard" or "first" (national rail only)

    Returns:
        dict with found, total_fare_usd (approximate), stations, legs
    """
    if network == "auto":
        network = "metro" if origin_id.upper().startswith("MS") else "rail"

    rel_type = "METRO_LINK" if network == "metro" else "RAIL_LINK"
    label    = "MetroStation" if network == "metro" else "NationalRailStation"

    # Step 1: find fewest-hop path in Neo4j (fewer stops = lower per-stop fare)
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH path = shortestPath(
                    (start:{label} {{station_id: $origin}})-[:{rel_type}*]->(end:{label} {{station_id: $destination}})
                )
                RETURN
                    [n IN nodes(path) | n.station_id]              AS station_ids,
                    [n IN nodes(path) | n.name]                    AS station_names,
                    [r IN relationships(path) | r.line]            AS lines,
                    [r IN relationships(path) | r.travel_time_min] AS segment_times,
                    length(path)                                   AS hop_count
                """,
                origin=origin_id,
                destination=destination_id,
            )
            row = result.single()
            if not row:
                return {"found": False, "origin_id": origin_id, "destination_id": destination_id}

            station_ids   = list(row["station_ids"])
            station_names = list(row["station_names"])
            lines         = list(row["lines"])
            segment_times = list(row["segment_times"])
            hop_count     = int(row["hop_count"])

    # Step 2: look up fare from PostgreSQL using the hop count
    total_fare_usd = None
    try:
        if network == "metro":
            from databases.relational.queries import query_metro_schedules, query_metro_fare
            schedules = query_metro_schedules(origin_id, destination_id)
            if schedules:
                fare_info = query_metro_fare(schedules[0]["schedule_id"], hop_count)
                if fare_info:
                    total_fare_usd = fare_info.get("total_fare_usd")
        else:
            from databases.relational.queries import (
                query_national_rail_availability,
                query_national_rail_fare,
            )
            schedules = query_national_rail_availability(origin_id, destination_id)
            if schedules:
                fare_info = query_national_rail_fare(
                    schedules[0]["schedule_id"], fare_class, hop_count
                )
                if fare_info:
                    total_fare_usd = fare_info.get("total_fare_usd")
    except Exception:
        pass

    stations = [
        {"station_id": sid, "name": sname}
        for sid, sname in zip(station_ids, station_names)
    ]
    legs = [
        {
            "from":            station_ids[i],
            "from_name":       station_names[i],
            "to":              station_ids[i + 1],
            "to_name":         station_names[i + 1],
            "line":            lines[i],
            "travel_time_min": segment_times[i],
        }
        for i in range(len(lines))
    ]

    return {
        "found":            True,
        "origin_id":        origin_id,
        "destination_id":   destination_id,
        "total_fare_usd":   total_fare_usd,
        "fare_approximate": True,
        "fare_class":       fare_class,
        "stations":         stations,
        "legs":             legs,
    }


# ── ALTERNATIVE ROUTES (avoiding a station) ───────────────────────────────────

def query_alternative_routes(
    origin_id: str,
    destination_id: str,
    avoid_station_id: str,
    network: str = "auto",
    max_routes: int = 3,
) -> list[list[dict]]:
    """
    Find paths between two stations that avoid a specific intermediate station.
    Useful for routing around a delayed or closed station.

    Args:
        origin_id:         e.g. "NR01"
        destination_id:    e.g. "NR05"
        avoid_station_id:  e.g. "NR03"
        network:           "metro", "rail", or "auto"
        max_routes:        max number of alternatives to return

    Returns:
        List of routes, each route is a list of leg dicts
    """
    if network == "auto":
        network = "metro" if origin_id.upper().startswith("MS") else "rail"

    rel_type = "METRO_LINK" if network == "metro" else "RAIL_LINK"
    label    = "MetroStation" if network == "metro" else "NationalRailStation"

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH path = (start:{label} {{station_id: $origin}})
                             -[:{rel_type}*1..12]->
                             (end:{label}   {{station_id: $destination}})
                WHERE NONE(n IN nodes(path) WHERE n.station_id = $avoid)
                  AND ALL(n IN nodes(path) WHERE single(m IN nodes(path) WHERE m = n))
                WITH path,
                     reduce(t = 0, r IN relationships(path) | t + r.travel_time_min) AS total_time
                ORDER BY total_time
                LIMIT {max_routes}
                RETURN
                    [n IN nodes(path) | n.station_id]              AS station_ids,
                    [n IN nodes(path) | n.name]                    AS station_names,
                    [r IN relationships(path) | r.line]            AS lines,
                    [r IN relationships(path) | r.travel_time_min] AS segment_times,
                    total_time
                """,
                origin=origin_id,
                destination=destination_id,
                avoid=avoid_station_id,
            )

            routes = []
            for row in result:
                station_ids   = list(row["station_ids"])
                station_names = list(row["station_names"])
                lines         = list(row["lines"])
                segment_times = list(row["segment_times"])

                legs = [
                    {
                        "from":            station_ids[i],
                        "from_name":       station_names[i],
                        "to":              station_ids[i + 1],
                        "to_name":         station_names[i + 1],
                        "line":            lines[i],
                        "travel_time_min": segment_times[i],
                    }
                    for i in range(len(lines))
                ]
                routes.append(legs)

            return routes


# ── CROSS-NETWORK INTERCHANGE PATH ───────────────────────────────────────────

def query_interchange_path(origin_id: str, destination_id: str) -> dict:
    """
    Find a path between a metro station and a national rail station (or vice versa)
    crossing the network boundary via interchange relationships.

    Args:
        origin_id:       e.g. "MS03" (metro) or "NR05" (national rail)
        destination_id:  e.g. "NR05" (national rail) or "MS09" (metro)

    Returns:
        dict with found, stations list, interchange points, total_time_min
    """
    origin_label = "MetroStation" if origin_id.upper().startswith("MS") else "NationalRailStation"
    dest_label   = "MetroStation" if destination_id.upper().startswith("MS") else "NationalRailStation"

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (start:{origin_label} {{station_id: $origin}})
                MATCH (end:{dest_label}     {{station_id: $destination}})
                CALL apoc.algo.dijkstra(start, end, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', 'travel_time_min', 5)
                YIELD path, weight
                RETURN
                    [n IN nodes(path) | n.station_id]              AS station_ids,
                    [n IN nodes(path) | n.name]                    AS station_names,
                    [n IN nodes(path) | labels(n)[0]]              AS node_labels,
                    [r IN relationships(path) | type(r)]           AS rel_types,
                    [r IN relationships(path) | r.travel_time_min] AS segment_times,
                    weight                                         AS total_time_min
                """,
                origin=origin_id,
                destination=destination_id,
            )
            row = result.single()
            if not row:
                return {"found": False, "origin_id": origin_id, "destination_id": destination_id}

            station_ids   = list(row["station_ids"])
            station_names = list(row["station_names"])
            node_labels   = list(row["node_labels"])
            rel_types     = list(row["rel_types"])
            segment_times = list(row["segment_times"])

            interchange_points = [
                {
                    "from_station_id": station_ids[i],
                    "from_name":       station_names[i],
                    "to_station_id":   station_ids[i + 1],
                    "to_name":         station_names[i + 1],
                }
                for i, rt in enumerate(rel_types)
                if rt == "INTERCHANGE_TO"
            ]

            stations = [
                {
                    "station_id": sid,
                    "name":       sname,
                    "network":    "metro" if lbl == "MetroStation" else "rail",
                }
                for sid, sname, lbl in zip(station_ids, station_names, node_labels)
            ]

            legs = [
                {
                    "from":            station_ids[i],
                    "from_name":       station_names[i],
                    "to":              station_ids[i + 1],
                    "to_name":         station_names[i + 1],
                    "connection_type": rel_types[i],
                    "travel_time_min": segment_times[i],
                }
                for i in range(len(rel_types))
            ]

            return {
                "found":              True,
                "origin_id":          origin_id,
                "destination_id":     destination_id,
                "total_time_min":     row["total_time_min"],
                "stations":           stations,
                "interchange_points": interchange_points,
                "legs":               legs,
            }


# ── DELAY RIPPLE ANALYSIS ─────────────────────────────────────────────────────

def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]:
    """
    Find all stations within N hops of a delayed or disrupted station.
    Works on both metro and national rail networks.

    Args:
        delayed_station_id: e.g. "NR03" or "MS01"
        hops:               how many connections out to search (default 2)

    Returns:
        List of dicts: {station_id, name, hops_away, lines_affected}
    """
    with _driver() as driver:
        with driver.session() as session:
            # hops is a trusted integer, safe to embed directly in the query
            result = session.run(
                f"""
                MATCH (disrupted {{station_id: $station_id}})
                MATCH path = (disrupted)-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..{hops}]-(affected)
                WHERE affected.station_id <> $station_id
                WITH affected, min(length(path)) AS hops_away
                RETURN
                    affected.station_id AS station_id,
                    affected.name       AS name,
                    hops_away,
                    affected.lines      AS lines_affected
                ORDER BY hops_away, affected.station_id
                """,
                station_id=delayed_station_id,
            )

            return [
                {
                    "station_id":     row["station_id"],
                    "name":           row["name"],
                    "hops_away":      row["hops_away"],
                    "lines_affected": list(row["lines_affected"]) if row["lines_affected"] else [],
                }
                for row in result
            ]


# ── STATION CONNECTIONS ───────────────────────────────────────────────────────

def query_station_connections(station_id: str) -> list[dict]:
    """
    List all direct connections from a given station.

    Args:
        station_id: e.g. "MS01" or "NR01"
    """
    label = "MetroStation" if station_id.upper().startswith("MS") else "NationalRailStation"

    with _driver() as driver:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (s:{label} {{station_id: $station_id}})-[r]->(n)
                RETURN
                    n.station_id      AS station_id,
                    n.name            AS name,
                    r.line            AS line,
                    r.travel_time_min AS travel_time_min
                ORDER BY r.travel_time_min IS NULL, r.travel_time_min
                """,
                station_id=station_id,
            )

            return [
                {
                    "station_id":      row["station_id"],
                    "name":            row["name"],
                    "line":            row["line"],
                    "travel_time_min": row["travel_time_min"],
                }
                for row in result
            ]
