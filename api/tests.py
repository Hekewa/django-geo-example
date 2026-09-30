from unittest.mock import patch
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase


class RouteOptimizationViewTests(APITestCase):

    def setUp(self):
        # Update 'api:route' if your urls.py namespace/name is different,
        # or use '/api/route/' directly.
        self.url = "/api/route/"

        # Mock geocoding coordinates [latitude, longitude]
        self.mock_start_coords = (25.7617, -80.1918)  # Miami, FL
        self.mock_end_coords = (38.9747, -92.7432)    # Boonville, MO

        # Mock OSRM geometry [longitude, latitude]
        self.mock_route_geometry = {
            "type": "LineString",
            "coordinates": [
                [-80.1918, 25.7617],  # Miami
                [-81.5158, 28.3772],  # Orlando area
                [-84.3880, 33.7490],  # Atlanta area
                [-86.7816, 36.1627],  # Nashville area
                [-90.1994, 38.6270],  # St. Louis area
                [-92.7432, 38.9747],  # Boonville
            ],
        }

        self.mock_osrm_response = {
            "geometry": self.mock_route_geometry,
            "duration": 65000.0,
            "distance": 1900000.0,
        }

    # =========================================================================
    # Validation & Parameter Tests
    # =========================================================================

    def test_missing_query_parameters(self):
        """Request without start and end parameters should return 400 Bad Request."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_missing_destination_parameter(self):
        """Request missing the 'end' parameter should return 400 Bad Request."""
        response = self.client.get(f"{self.url}?start=Miami")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_conflicting_fuel_parameters(self):
        """Providing both fuel_range_miles and fuel_gallons should return 400."""
        response = self.client.get(
            f"{self.url}?start=Miami&end=Boonville&fuel_range_miles=200&fuel_gallons=20"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Specify either", response.data["error"])

    def test_invalid_fuel_gallons(self):
        """Negative fuel or fuel exceeding tank capacity should return 400."""
        response_neg = self.client.get(
            f"{self.url}?start=Miami&end=Boonville&fuel_gallons=-10"
        )
        self.assertEqual(response_neg.status_code, status.HTTP_400_BAD_REQUEST)

        response_exceed = self.client.get(
            f"{self.url}?start=Miami&end=Boonville&fuel_gallons=100"
        )
        self.assertEqual(response_exceed.status_code, status.HTTP_400_BAD_REQUEST)

    # =========================================================================
    # Geocoding & Routing Error Handling Tests
    # =========================================================================

    @patch("api.views.RouteOptimizationView._geocode")
    def test_geocode_failure(self, mock_geocode):
        """If geocoding returns None for a location, return 400 Bad Request."""
        mock_geocode.return_value = None
        response = self.client.get(f"{self.url}?start=InvalidPlace123&end=Boonville")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unable to geocode", response.data["error"])

    @patch("api.views.RouteOptimizationView._geocode")
    @patch("api.views.RouteOptimizationView._get_osrm_route")
    def test_osrm_route_failure(self, mock_route, mock_geocode):
        """If OSRM fails to return a route, return 400 Bad Request."""
        mock_geocode.side_effect = [self.mock_start_coords, self.mock_end_coords]
        mock_route.return_value = None

        response = self.client.get(f"{self.url}?start=Miami&end=Boonville")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unable to calculate driving route", response.data["error"])

    # =========================================================================
    # Successful Route Optimization Tests
    # =========================================================================

    @patch("api.views.RouteOptimizationView._geocode")
    @patch("api.views.RouteOptimizationView._get_osrm_route")
    def test_successful_json_response(self, mock_route, mock_geocode):
        """Valid request with default JSON Accept header returns route optimization JSON payload."""
        mock_geocode.side_effect = [self.mock_start_coords, self.mock_end_coords]
        mock_route.return_value = self.mock_osrm_response

        response = self.client.get(f"{self.url}?start=Miami&end=Boonville")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/json")

        # Verify JSON keys
        self.assertIn("summary", response.data)
        self.assertIn("start", response.data)
        self.assertIn("destination", response.data)
        self.assertIn("fuel_stops", response.data)
        self.assertIn("route_geometry", response.data)

        # Summary checks
        summary = response.data["summary"]
        self.assertGreater(summary["total_distance_miles"], 0)
        self.assertGreater(summary["total_fuel_cost_usd"], 0)
        self.assertEqual(summary["mpg"], 10.0)

    @patch("api.views.RouteOptimizationView._geocode")
    @patch("api.views.RouteOptimizationView._get_osrm_route")
    def test_successful_html_browser_response(self, mock_route, mock_geocode):
        """Browser requests sending Accept: text/html receive an interactive HTML dashboard."""
        mock_geocode.side_effect = [self.mock_start_coords, self.mock_end_coords]
        mock_route.return_value = self.mock_osrm_response

        response = self.client.get(
            f"{self.url}?start=Miami&end=Boonville",
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response["Content-Type"].startswith("text/html"))

        html_content = response.content.decode("utf-8")
        self.assertIn("<!DOCTYPE html>", html_content)
        self.assertIn("Fuel Route Optimizer", html_content)
        self.assertIn("plotly", html_content.lower())

    @patch("api.views.RouteOptimizationView._geocode")
    @patch("api.views.RouteOptimizationView._get_osrm_route")
    def test_format_html_override(self, mock_route, mock_geocode):
        """Passing ?format=html explicitly forces an HTML response regardless of Accept header."""
        mock_geocode.side_effect = [self.mock_start_coords, self.mock_end_coords]
        mock_route.return_value = self.mock_osrm_response

        response = self.client.get(f"{self.url}?start=Miami&end=Boonville&format=html")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response["Content-Type"].startswith("text/html"))