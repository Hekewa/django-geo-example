import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from django.http import HttpResponse
from pathlib import Path

from api.views import RouteOptimizationView

def truckstop_map(request):
    """A quick visualization to make sure the truckstop_coords.csv I generated makes sense"""
    # CSV files located in the same directory as this views.py file.
    base_dir = Path(__file__).resolve().parent

    coords_path = base_dir / "truckstop_coords.csv"
    prices_path = base_dir / "fuel_prices.csv"

    # Load CSV files
    df_coords = pd.read_csv(coords_path)
    df_prices = pd.read_csv(prices_path).drop_duplicates(
        subset=["OPIS Truckstop ID"]
    )

    # Merge datasets
    merged = pd.merge(
        df_coords,
        df_prices,
        on="OPIS Truckstop ID",
        how="left",
    )

    # Fill missing text fields
    merged["Truckstop Name"] = merged["Truckstop Name"].fillna(
        "No Price Data Available"
    )


    # Create Plotly map
    fig = px.scatter_map(
        merged,
        lat="Latitude",
        lon="Longitude",
        color="Retail Price",
        hover_name="Truckstop Name",
        hover_data={
            "OPIS Truckstop ID": True,
            "Address": True,
            "City": True,
            "State": True,
            "Retail Price": ":.3f",
            "Latitude": False,
            "Longitude": False,
        },
        zoom=3.5,
        center={
            "lat": 39.8283,
            "lon": -98.5795,
        },
        title="US Truckstop Locations & Fuel Prices with Roads",
        color_continuous_scale="Viridis",
    )

    start_coords = (25.7741566, -80.1935973)
    end_coords = (38.9751547, -92.7440277)
    route_data = RouteOptimizationView._get_osrm_route(start_coords, end_coords)
    if route_data and "geometry" in route_data:
        # Extract the coordinate list from the GeoJSON geometry
        coords = route_data["geometry"]["coordinates"]
        
        # GeoJSON coordinates are returned as [longitude, latitude]
        route_lons = [c[0] for c in coords]
        route_lats = [c[1] for c in coords]
        
        # Add the route line beneath your truck stop scatter points
        fig.add_trace(
            go.Scattermap(
                mode="lines",
                lon=route_lons,
                lat=route_lats,
                line=dict(width=4, color="blue"),
                name="Route",
                hoverinfo="none" # Prevents tooltip interference with the route line
            )
        )

    fig.update_layout(
        margin=dict(l=0, r=0, t=40, b=0),
        map=dict(
            style="carto-positron",
            center=dict(
                lat=39.8283,
                lon=-98.5795,
            ),
            zoom=3.5,
        ),
    )

    # Convert Plotly figure to HTML
    plot_html = fig.to_html(
        full_html=True,
        include_plotlyjs=True,
    )
    return HttpResponse(plot_html)