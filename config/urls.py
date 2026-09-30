from django.contrib import admin
from django.urls import include, path

from api.views import RouteOptimizationView
from api.visualize import truckstop_map

urlpatterns = [
    path('api/route/', RouteOptimizationView.as_view(), name='route_optimization'),
    path("truckstop_map", truckstop_map, name="truckstop_map"),
]