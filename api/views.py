import bisect
import math
import os
import logging
from functools import lru_cache

import numpy as np
import pandas as pd
import requests
import plotly.graph_objects as go

from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.renderers import JSONRenderer, StaticHTMLRenderer
from scipy.spatial import cKDTree

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

MAX_RANGE_MILES = 500.0
MPG = 10.0
TANK_CAPACITY_GALLONS = MAX_RANGE_MILES / MPG

ROUTE_CORRIDOR_MILES = 10.0
ROUTE_SAMPLE_MILES = 10.0
EARTH_RADIUS_MILES = 3958.7613

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "DjangoFuelRouteOptimizer/1.0"


# =============================================================================
# Data loading & Geographic Helpers
# =============================================================================

@lru_cache(maxsize=1)
def load_truck_stops():
    fuel_csv = os.path.join(settings.BASE_DIR, "api/fuel_prices.csv")
    coords_csv = os.path.join(settings.BASE_DIR, "api/truckstop_coords.csv")

    if not os.path.exists(fuel_csv) or not os.path.exists(coords_csv):
        raise RuntimeError("Fuel price or coordinate CSV file missing.")

    fuel_df = pd.read_csv(fuel_csv)
    coords_df = pd.read_csv(coords_csv)

    fuel_df["OPIS Truckstop ID"] = pd.to_numeric(fuel_df["OPIS Truckstop ID"], errors="coerce")
    coords_df["OPIS Truckstop ID"] = pd.to_numeric(coords_df["OPIS Truckstop ID"], errors="coerce")
    fuel_df["Retail Price"] = pd.to_numeric(fuel_df["Retail Price"], errors="coerce")
    coords_df["Latitude"] = pd.to_numeric(coords_df["Latitude"], errors="coerce")
    coords_df["Longitude"] = pd.to_numeric(coords_df["Longitude"], errors="coerce")

    fuel_df = fuel_df.dropna(subset=["OPIS Truckstop ID", "Retail Price"]).drop_duplicates(subset=["OPIS Truckstop ID"], keep="last")
    coords_df = coords_df.dropna(subset=["OPIS Truckstop ID", "Latitude", "Longitude"]).drop_duplicates(subset=["OPIS Truckstop ID"], keep="first")

    stations = fuel_df.merge(coords_df, on="OPIS Truckstop ID", how="inner").reset_index(drop=True)
    tree = cKDTree(np.radians(stations[["Latitude", "Longitude"]].to_numpy()))

    return stations, tree


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def build_route_distances(route_coordinates):
    cumulative = [0.0]
    for i in range(1, len(route_coordinates)):
        lon1, lat1 = route_coordinates[i - 1]
        lon2, lat2 = route_coordinates[i]
        cumulative.append(cumulative[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return cumulative


def interpolate_route_coordinate(route_coordinates, cumulative_distances, distance_miles):
    if distance_miles <= 0:
        return route_coordinates[0]
    if distance_miles >= cumulative_distances[-1]:
        return route_coordinates[-1]

    index = bisect.bisect_left(cumulative_distances, distance_miles)
    if index == 0:
        return route_coordinates[0]

    prev_d, next_d = cumulative_distances[index - 1], cumulative_distances[index]
    if next_d == prev_d:
        return route_coordinates[index]

    ratio = (distance_miles - prev_d) / (next_d - prev_d)
    lon1, lat1 = route_coordinates[index - 1]
    lon2, lat2 = route_coordinates[index]
    return lon1 + (lon2 - lon1) * ratio, lat1 + (lat2 - lat1) * ratio


def nearest_route_position(latitude, longitude, route_coordinates, cumulative_distances, route_tree):
    station_radians = np.radians([longitude, latitude])
    _, nearest_indices = route_tree.query(station_radians, k=min(5, len(route_coordinates)))
    nearest_indices = np.atleast_1d(nearest_indices)

    candidate_segments = set()
    for vertex_index in nearest_indices:
        v_idx = int(vertex_index)
        if v_idx > 0:
            candidate_segments.add(v_idx - 1)
        if v_idx < len(route_coordinates) - 1:
            candidate_segments.add(v_idx)

    best_dist_from, best_dist_along = float("inf"), None
    lat0, lon0 = math.radians(latitude), math.radians(longitude)
    cos_lat0 = math.cos(lat0)

    for seg_idx in candidate_segments:
        lon1, lat1 = route_coordinates[seg_idx]
        lon2, lat2 = route_coordinates[seg_idx + 1]

        ax = (math.radians(lon1) - lon0) * cos_lat0 * EARTH_RADIUS_MILES
        ay = (math.radians(lat1) - lat0) * EARTH_RADIUS_MILES
        bx = (math.radians(lon2) - lon0) * cos_lat0 * EARTH_RADIUS_MILES
        by = (math.radians(lat2) - lat0) * EARTH_RADIUS_MILES

        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))

        px, py = ax + t * dx, ay + t * dy
        dist_from = math.sqrt(px * px + py * py)
        dist_along = cumulative_distances[seg_idx] + t * (cumulative_distances[seg_idx + 1] - cumulative_distances[seg_idx])

        if dist_from < best_dist_from:
            best_dist_from = dist_from
            best_dist_along = dist_along

    return best_dist_along, best_dist_from


# =============================================================================
# API View
# =============================================================================

class RouteOptimizationView(APIView):

    renderer_classes = [JSONRenderer, StaticHTMLRenderer]

    def get(self, request):
        start_address = request.query_params.get("start")
        end_address = request.query_params.get("end")

        if not start_address or not end_address:
            return Response(
                {"error": "Please provide both 'start' and 'end' query parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            initial_fuel_gallons = self._parse_initial_fuel(request)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            start_coords = self._geocode(start_address)
            if not start_coords:
                return Response(
                    {"error": f"Unable to geocode start location '{start_address}'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            end_coords = self._geocode(end_address)
            if not end_coords:
                return Response(
                    {"error": f"Unable to geocode destination '{end_address}'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            route = self._get_osrm_route(start_coords, end_coords)
            if not route:
                return Response(
                    {"error": "Unable to calculate driving route."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            route_geometry = route["geometry"]
            route_coordinates = route_geometry["coordinates"]
            cumulative_distances = build_route_distances(route_coordinates)
            total_distance = cumulative_distances[-1]

            stations, station_tree = load_truck_stops()

            route_stations = self._find_stations_near_route(
                stations=stations,
                station_tree=station_tree,
                route_coordinates=route_coordinates,
                cumulative_distances=cumulative_distances,
            )

            fuel_stops, total_cost = self._optimize_fuel(
                route_stations=route_stations,
                total_distance=total_distance,
                initial_fuel_gallons=initial_fuel_gallons,
                max_range=MAX_RANGE_MILES,
                mpg=MPG,
            )

            payload = {
                "summary": {
                    "total_distance_miles": round(total_distance, 2),
                    "total_gallons_consumed": round(total_distance / MPG, 2),
                    "initial_fuel_gallons": round(initial_fuel_gallons, 2),
                    "initial_fuel_range_miles": round(initial_fuel_gallons * MPG, 2),
                    "tank_capacity_gallons": round(TANK_CAPACITY_GALLONS, 2),
                    "maximum_range_miles": MAX_RANGE_MILES,
                    "mpg": MPG,
                    "total_fuel_cost_usd": round(total_cost, 2),
                    "fuel_stops_count": len(fuel_stops),
                },
                "start": {
                    "address": start_address,
                    "coordinates": {"lat": start_coords[0], "lon": start_coords[1]},
                },
                "destination": {
                    "address": end_address,
                    "coordinates": {"lat": end_coords[0], "lon": end_coords[1]},
                },
                "fuel_stops": fuel_stops,
                "route_geometry": route_geometry,
            }

            # --- Content Negotiation: Check if request favors HTML (Browser) ---
            accept_header = request.headers.get("Accept", "")
            wants_html = "text/html" in accept_header and (
                "application/json" not in accept_header or
                accept_header.find("text/html") < accept_header.find("application/json")
            )

            if wants_html or request.query_params.get("format") == "html":
                html_page = self._render_html_dashboard(payload)
                return HttpResponse(html_page, content_type="text/html")

            return Response(payload)

        except requests.RequestException as exc:
            return Response(
                {"error": f"External service unavailable: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except RuntimeError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    # =========================================================================
    # HTML Visualizer Method
    # =========================================================================

    @staticmethod
    def _render_html_dashboard(payload):
        summary = payload["summary"]
        start = payload["start"]
        dest = payload["destination"]
        fuel_stops = payload["fuel_stops"]
        route_coords = payload["route_geometry"]["coordinates"]

        route_lons = [c[0] for c in route_coords]
        route_lats = [c[1] for c in route_coords]

        fig = go.Figure()

        # 1. Route Line
        fig.add_trace(
            go.Scattermap(
                mode="lines",
                lon=route_lons,
                lat=route_lats,
                line=dict(width=4, color="#2563eb"),
                name="Route",
                hoverinfo="none",
            )
        )

        # 2. Start & Destination Markers
        fig.add_trace(
            go.Scattermap(
                mode="markers+text",
                lon=[start["coordinates"]["lon"], dest["coordinates"]["lon"]],
                lat=[start["coordinates"]["lat"], dest["coordinates"]["lat"]],
                marker=dict(size=14, color=["#16a34a", "#dc2626"]),
                text=["Start", "Destination"],
                textposition="top center",
                hoverinfo="text",
                hovertext=[f"Start: {start['address']}", f"Destination: {dest['address']}"],
                name="Endpoints",
            )
        )

        # 3. Optimal Fuel Stops
        if fuel_stops:
            stop_lons = [s["coordinates"]["lon"] for s in fuel_stops]
            stop_lats = [s["coordinates"]["lat"] for s in fuel_stops]
            stop_hover = [
                f"<b>{s['truckstop_name']}</b><br>"
                f"{s['address']}, {s['city']}, {s['state']}<br>"
                f"Price: ${s['price_per_gallon']:.3f}/gal<br>"
                f"Bought: {s['gallons_purchased']} gal (${s['cost_usd']:.2f})<br>"
                f"Distance: {s['distance_from_start_miles']} mi from start"
                for s in fuel_stops
            ]

            fig.add_trace(
                go.Scattermap(
                    mode="markers",
                    lon=stop_lons,
                    lat=stop_lats,
                    marker=dict(size=12, color="#eab308"),
                    hoverinfo="text",
                    hovertext=stop_hover,
                    name="Fuel Stops",
                )
            )

        center_lat = (start["coordinates"]["lat"] + dest["coordinates"]["lat"]) / 2
        center_lon = (start["coordinates"]["lon"] + dest["coordinates"]["lon"]) / 2

        fig.update_layout(
            margin=dict(l=0, r=0, t=0, b=0),
            map=dict(
                style="open-street-map",
                center=dict(lat=center_lat, lon=center_lon),
                zoom=4.5,
            ),
            showlegend=True,
            legend=dict(x=0.01, y=0.99, bgcolor="rgba(255, 255, 255, 0.8)"),
        )

        plotly_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

        # Create styled dashboard layout
        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fuel Route Optimizer: {start['address']} to {dest['address']}</title>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 0; background-color: #f8fafc; }}
        .header {{ background: #1e293b; color: white; padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; }}
        .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; padding: 1rem 2rem; background: white; border-bottom: 1px solid #e2e8f0; }}
        .card {{ background: #f1f5f9; padding: 0.75rem 1rem; border-radius: 8px; }}
        .card .label {{ font-size: 0.75rem; color: #64748b; text-transform: uppercase; font-weight: 600; }}
        .card .value {{ font-size: 1.25rem; font-weight: 700; color: #0f172a; margin-top: 0.25rem; }}
        .content {{ display: flex; height: calc(100vh - 160px); }}
        .sidebar {{ width: 320px; overflow-y: auto; background: white; border-right: 1px solid #e2e8f0; padding: 1rem; }}
        .map-container {{ flex: 1; height: 100%; }}
        .stop-item {{ border: 1px solid #e2e8f0; padding: 0.75rem; border-radius: 6px; margin-bottom: 0.75rem; background: #fff; }}
        .stop-item .name {{ font-weight: 600; color: #1e293b; }}
        .stop-item .details {{ font-size: 0.85rem; color: #64748b; margin-top: 0.25rem; }}
    </style>
</head>
<body>
    <div class="header">
        <h2 style="margin:0;">🚗 Fuel Route Optimizer</h2>
        <div><b>{start['address']}</b> &rarr; <b>{dest['address']}</b></div>
    </div>
    <div class="metrics">
        <div class="card"><div class="label">Total Distance</div><div class="value">{summary['total_distance_miles']} mi</div></div>
        <div class="card"><div class="label">Total Fuel Cost</div><div class="value">${summary['total_fuel_cost_usd']:.2f}</div></div>
        <div class="card"><div class="label">Gallons Used</div><div class="value">{summary['total_gallons_consumed']} gal</div></div>
        <div class="card"><div class="label">Fuel Stops</div><div class="value">{summary['fuel_stops_count']}</div></div>
    </div>
    <div class="content">
        <div class="sidebar">
            <h3 style="margin-top:0;">Optimal Fuel Stops</h3>
            {"".join([f'''
            <div class="stop-item">
                <div class="name">{i+1}. {s["truckstop_name"]}</div>
                <div class="details">
                    {s["address"]}, {s["city"]}, {s["state"]}<br>
                    <b>Price:</b> ${s["price_per_gallon"]:.3f}/gal<br>
                    <b>Purchase:</b> {s["gallons_purchased"]} gal (${s["cost_usd"]:.2f})<br>
                    <b>Distance:</b> {s["distance_from_start_miles"]} mi
                </div>
            </div>
            ''' for i, s in enumerate(fuel_stops)]) if fuel_stops else '<p>No intermediate fuel stops required!</p>'}
        </div>
        <div class="map-container">
            {plotly_html}
        </div>
    </div>
</body>
</html>"""
        return html_content

    # =========================================================================
    # Parsing, Geocoding & Optimization logic
    # =========================================================================

    @staticmethod
    def _parse_initial_fuel(request):
        range_val = request.query_params.get("fuel_range_miles")
        gallons_val = request.query_params.get("fuel_gallons")

        if range_val is not None and gallons_val is not None:
            raise ValueError("Specify either 'fuel_range_miles' or 'fuel_gallons', not both.")
        if range_val is None and gallons_val is None:
            return TANK_CAPACITY_GALLONS

        if range_val is not None:
            fuel_range = float(range_val)
            if not math.isfinite(fuel_range) or fuel_range < 0 or fuel_range > MAX_RANGE_MILES:
                raise ValueError("Invalid 'fuel_range_miles' value.")
            return fuel_range / MPG

        fuel_gallons = float(gallons_val)
        if not math.isfinite(fuel_gallons) or fuel_gallons < 0 or fuel_gallons > TANK_CAPACITY_GALLONS:
            raise ValueError("Invalid 'fuel_gallons' value.")
        return fuel_gallons

    @staticmethod
    def _geocode(address):
        response = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": USER_AGENT},
            timeout=5,
        )
        response.raise_for_status()
        res = response.json()
        return (float(res[0]["lat"]), float(res[0]["lon"])) if res else None

    @staticmethod
    def _get_osrm_route(start, end):
        url = f"{OSRM_URL}/{start[1]},{start[0]};{end[1]},{end[0]}"
        response = requests.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data["routes"][0] if data.get("code") == "Ok" and data.get("routes") else None

    @staticmethod
    def _find_stations_near_route(stations, station_tree, route_coordinates, cumulative_distances):
        route_tree = cKDTree(np.radians(np.array(route_coordinates)))
        total_distance = cumulative_distances[-1]

        sample_distances = np.arange(0.0, total_distance, ROUTE_SAMPLE_MILES)
        if len(sample_distances) == 0 or sample_distances[-1] < total_distance:
            sample_distances = np.append(sample_distances, total_distance)

        matched_indices = set()
        query_radius = (ROUTE_CORRIDOR_MILES / EARTH_RADIUS_MILES) * 1.7

        for distance in sample_distances:
            lon, lat = interpolate_route_coordinate(route_coordinates, cumulative_distances, float(distance))
            nearby_indices = station_tree.query_ball_point(np.radians([lat, lon]), r=query_radius)
            matched_indices.update(nearby_indices)

        result = {}
        for station_index in matched_indices:
            row = stations.iloc[station_index]
            station_lat, station_lon = float(row["Latitude"]), float(row["Longitude"])

            dist_along, dist_from = nearest_route_position(
                latitude=station_lat,
                longitude=station_lon,
                route_coordinates=route_coordinates,
                cumulative_distances=cumulative_distances,
                route_tree=route_tree,
            )

            if dist_from > ROUTE_CORRIDOR_MILES:
                continue

            opis_id = int(row["OPIS Truckstop ID"])
            station = {
                "opis_id": opis_id,
                "name": str(row["Truckstop Name"]).strip(),
                "address": str(row["Address"]).strip(),
                "city": str(row["City"]).strip(),
                "state": str(row["State"]).strip(),
                "price": float(row["Retail Price"]),
                "dist": float(max(0.0, min(total_distance, dist_along))),
                "distance_from_route_miles": float(dist_from),
                "lat": station_lat,
                "lon": station_lon,
            }

            previous = result.get(opis_id)
            if previous is None or station["distance_from_route_miles"] < previous["distance_from_route_miles"]:
                result[opis_id] = station

        return sorted(list(result.values()), key=lambda s: s["dist"])

    @staticmethod
    def _optimize_fuel(route_stations, total_distance, initial_fuel_gallons, max_range, mpg):
        tank_capacity_gallons = max_range / mpg
        if total_distance <= 0 or total_distance <= (initial_fuel_gallons * mpg) + 1e-9:
            return [], 0.0

        stations = sorted(
            [s for s in route_stations if 1e-9 < float(s["dist"]) < total_distance - 1e-9],
            key=lambda s: s["dist"],
        )

        candidate_first = [(i, s) for i, s in enumerate(stations) if s["dist"] <= (initial_fuel_gallons * mpg) + 1e-9]
        if not candidate_first:
            raise ValueError(f"No fuel station is reachable within initial range.")

        def simulate_trip(start_index):
            curr_index = start_index
            curr_station = stations[curr_index]
            curr_dist = curr_station["dist"]
            fuel_remaining = initial_fuel_gallons - (curr_dist / mpg)
            total_cost, fuel_stops = 0.0, []

            while True:
                if fuel_remaining * mpg >= (total_distance - curr_dist) - 1e-9:
                    break

                curr_price = float(curr_station["price"])
                cheaper_index = None

                for cand_idx in range(curr_index + 1, len(stations)):
                    cand = stations[cand_idx]
                    if cand["dist"] - curr_dist > max_range + 1e-9:
                        break
                    if float(cand["price"]) < curr_price:
                        cheaper_index = cand_idx
                        break

                if cheaper_index is not None:
                    target = stations[cheaper_index]
                    dist_to_target = target["dist"] - curr_dist
                    gallons_to_buy = max(0.0, (dist_to_target / mpg) - fuel_remaining)

                    if gallons_to_buy > 1e-9:
                        cost = gallons_to_buy * curr_price
                        fuel_remaining += gallons_to_buy
                        total_cost += cost
                        fuel_stops.append(RouteOptimizationView._serialize_fuel_stop(curr_station, gallons_to_buy, cost))

                    fuel_remaining -= dist_to_target / mpg
                    curr_dist, curr_index, curr_station = target["dist"], cheaper_index, target
                    continue

                gallons_to_buy = tank_capacity_gallons - fuel_remaining
                if gallons_to_buy > 1e-9:
                    cost = gallons_to_buy * curr_price
                    fuel_remaining += gallons_to_buy
                    total_cost += cost
                    fuel_stops.append(RouteOptimizationView._serialize_fuel_stop(curr_station, gallons_to_buy, cost))

                if fuel_remaining * mpg >= (total_distance - curr_dist) - 1e-9:
                    break

                reachable = [
                    (c_idx, c) for c_idx, c in enumerate(stations[curr_index + 1:], start=curr_index + 1)
                    if c["dist"] - curr_dist <= max_range + 1e-9
                ]

                if not reachable:
                    return None, float("inf")

                next_index, next_station = min(reachable, key=lambda x: (float(x[1]["price"]), x[1]["dist"]))
                fuel_remaining -= (next_station["dist"] - curr_dist) / mpg
                curr_dist, curr_index, curr_station = next_station["dist"], next_index, next_station

            return fuel_stops, total_cost

        best_stops, min_cost = None, float("inf")
        for idx, _ in candidate_first:
            stops, cost = simulate_trip(idx)
            if stops is not None and cost < min_cost:
                min_cost, best_stops = cost, stops

        return best_stops, min_cost

    @staticmethod
    def _serialize_fuel_stop(station, gallons, cost):
        return {
            "opis_id": int(station["opis_id"]),
            "truckstop_name": station["name"],
            "address": station["address"],
            "city": station["city"],
            "state": station["state"],
            "price_per_gallon": round(float(station["price"]), 3),
            "gallons_purchased": round(float(gallons), 3),
            "cost_usd": round(float(cost), 2),
            "distance_from_start_miles": round(float(station["dist"]), 2),
            "distance_from_route_miles": round(float(station["distance_from_route_miles"]), 2),
            "coordinates": {"lat": float(station["lat"]), "lon": float(station["lon"])},
        }