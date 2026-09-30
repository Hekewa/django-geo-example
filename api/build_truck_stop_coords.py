import os
import sys
import time
import requests
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FUEL_CSV = os.path.join(BASE_DIR, 'fuel_prices.csv')
COORDS_CSV = os.path.join(BASE_DIR, 'truckstop_coords.csv')

# Retrieve API key from environment variable or replace directly here
MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY", "YOUR_MAPBOX_API_KEY_HERE")

def save_progress(existing_coords, new_results):
    """Merges newly geocoded results into truckstop_coords.csv and flushes to disk."""
    if not new_results:
        return existing_coords

    new_df = pd.DataFrame(new_results)
    updated_coords = pd.concat([existing_coords, new_df], ignore_index=True)
    updated_coords = updated_coords.dropna(subset=['Latitude', 'Longitude']).drop_duplicates(subset=['OPIS Truckstop ID'])
    updated_coords.to_csv(COORDS_CSV, index=False)
    print(f"💾 Flushed progress: {len(updated_coords)} total truck stop coordinates in {COORDS_CSV}")
    return updated_coords

def geocode_mapbox(query, api_key):
    """
    Queries Mapbox Places API.
    Returns (latitude, longitude) or (None, None).
    Note: Mapbox returns coordinates in [longitude, latitude] order.
    """
    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{requests.utils.quote(query)}.json"
    params = {
        'access_token': api_key,
        'country': 'us',
        'limit': 1
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            features = data.get('features', [])
            if features:
                # Mapbox returns [longitude, latitude]
                lon, lat = features[0]['center']
                return float(lat), float(lon)
        elif response.status_code == 401:
            print("\n❌ Invalid Mapbox API Key! Please check your access token.")
            sys.exit(1)
    except Exception as e:
        print(f"    ⚠️ Request error for '{query}': {e}")

    return None, None

def generate_truckstop_coordinates():
    if MAPBOX_API_KEY == "YOUR_MAPBOX_API_KEY_HERE":
        print("❌ Please set your Mapbox API key in the script or pass it via environment variable:")
        print("   export MAPBOX_API_KEY='your_actual_token'")
        return

    if not os.path.exists(FUEL_CSV):
        print(f"❌ Error: {FUEL_CSV} not found.")
        return

    print("📖 Reading fuel prices CSV...")
    fuel_df = pd.read_csv(FUEL_CSV)
    
    fuel_df['OPIS Truckstop ID'] = fuel_df['OPIS Truckstop ID'].astype(int)
    fuel_df['Address'] = fuel_df['Address'].astype(str).str.strip()
    fuel_df['City'] = fuel_df['City'].astype(str).str.strip()
    fuel_df['State'] = fuel_df['State'].astype(str).str.strip()

    # Get unique truck stops by OPIS ID
    unique_stops = fuel_df.drop_duplicates(subset=['OPIS Truckstop ID']).reset_index(drop=True)
    print(f"Found {len(unique_stops)} unique OPIS Truckstop IDs.")

    # Load existing progress if file exists
    existing_coords = pd.DataFrame(columns=['OPIS Truckstop ID', 'Latitude', 'Longitude'])
    if os.path.exists(COORDS_CSV):
        print("🔍 Checking existing truckstop_coords.csv for resolved IDs...")
        existing_coords = pd.read_csv(COORDS_CSV)
        existing_coords['OPIS Truckstop ID'] = existing_coords['OPIS Truckstop ID'].astype(int)
        
        merged = pd.merge(unique_stops, existing_coords, on='OPIS Truckstop ID', how='left')
        missing_stops = merged[merged['Latitude'].isna()].reset_index(drop=True)
    else:
        missing_stops = unique_stops

    if missing_stops.empty:
        print("✅ All OPIS Truckstop IDs are already geocoded!")
        return

    print(f"🚀 Geocoding {len(missing_stops)} remaining truck stops using Mapbox API...")
    print("💡 You can press Ctrl+C at any time; progress will be saved automatically.\n")

    new_results = []
    
    try:
        for idx, row in missing_stops.iterrows():
            opis_id = row['OPIS Truckstop ID']
            address = row['Address']
            city = row['City']
            state = row['State']
            
            primary_query = f"{address}, {city}, {state}, USA"
            fallback_query = f"{city}, {state}, USA"
            
            print(f"[{idx + 1}/{len(missing_stops)}] ID {opis_id}: Geocoding '{primary_query}'")
            
            # Primary address lookup
            lat, lon = geocode_mapbox(primary_query, MAPBOX_API_KEY)
            
            # Fallback to City, State if full highway exit address isn't found
            if lat is None or lon is None:
                print(f"    ⚠️ Full address not found, falling back to: '{fallback_query}'")
                lat, lon = geocode_mapbox(fallback_query, MAPBOX_API_KEY)

            if lat is not None and lon is not None:
                new_results.append({
                    'OPIS Truckstop ID': opis_id,
                    'Latitude': lat,
                    'Longitude': lon
                })
            else:
                print(f"    ❌ Failed to geocode ID {opis_id}")

            # Save every 50 items (Mapbox is very fast)
            if len(new_results) >= 50:
                existing_coords = save_progress(existing_coords, new_results)
                new_results = []

    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user (Ctrl+C)!")
    except Exception as e:
        print(f"\n\n❌ Unexpected error encountered: {e}")
    finally:
        if new_results:
            print("\n💾 Flushing remaining progress to CSV before exit...")
            save_progress(existing_coords, new_results)
        print("✅ Process closed safely.")

if __name__ == '__main__':
    generate_truckstop_coordinates()