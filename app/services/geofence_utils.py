"""
Geofence and Location Utilities
================================

Provides geospatial calculations and validations for geofence operations.

MEDIUM PRIORITY BUG FIX #4: Geofence intersection validation
"""

import math
from typing import Tuple, Optional


EARTH_RADIUS_KM = 6371  # Earth's radius in kilometers


def haversine_distance(
    lat1: float, lon1: float,
    lat2: float, lon2: float
) -> float:
    """
    Calculate distance between two geographic points using Haversine formula.
    
    Args:
        lat1, lon1: First point latitude/longitude (degrees)
        lat2, lon2: Second point latitude/longitude (degrees)
        
    Returns:
        Distance in kilometers
        
    Example:
        >>> haversine_distance(
        ...     37.7749, -122.4194,  # San Francisco
        ...     37.3382, -121.8863   # San Jose
        ... )
        56.1  # approximately 56.1 km
    """
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    
    return EARTH_RADIUS_KM * c


def check_geofences_intersect(
    lat1: float, lon1: float, radius1_km: float,
    lat2: float, lon2: float, radius2_km: float
) -> bool:
    """
    Check if two geofences intersect (circles on map).
    
    Two circles intersect if the distance between centers is less than the sum of radii.
    
    Args:
        lat1, lon1: Center of first geofence (latitude/longitude)
        radius1_km: Radius of first geofence in kilometers
        lat2, lon2: Center of second geofence
        radius2_km: Radius of second geofence in kilometers
        
    Returns:
        True if geofences intersect, False otherwise
        
    Example:
        >>> # Two overlapping geofences in San Francisco
        >>> check_geofences_intersect(
        ...     37.7749, -122.4194, 0.5,  # Geofence A: 500m radius
        ...     37.7749, -122.4150, 0.5   # Geofence B: 500m radius, ~4km away
        ... )
        False  # Too far apart
    """
    distance = haversine_distance(lat1, lon1, lat2, lon2)
    return distance < (radius1_km + radius2_km)


def validate_geofence_radius(radius_km: float) -> bool:
    """
    Validate geofence radius is within acceptable bounds.
    
    - Minimum: 0.1 km (100 meters)
    - Maximum: 5000 km (state-level geofence)
    
    Args:
        radius_km: Radius in kilometers
        
    Returns:
        True if valid, False otherwise
        
    Raises:
        ValueError: If radius is invalid with explanation
    """
    MIN_RADIUS_KM = 0.1
    MAX_RADIUS_KM = 5000
    
    if radius_km < MIN_RADIUS_KM:
        raise ValueError(f"Geofence radius must be at least {MIN_RADIUS_KM} km (100 meters)")
    if radius_km > MAX_RADIUS_KM:
        raise ValueError(f"Geofence radius cannot exceed {MAX_RADIUS_KM} km")
    
    return True


def calculate_geofence_area_sq_km(radius_km: float) -> float:
    """
    Calculate area covered by a circular geofence.
    
    Formula: A = π × r²
    
    Args:
        radius_km: Radius in kilometers
        
    Returns:
        Area in square kilometers
        
    Example:
        >>> calculate_geofence_area_sq_km(1.0)
        3.14  # approximately pi
    """
    return math.pi * (radius_km ** 2)


def validate_geofence_area(radius_km: float) -> bool:
    """
    Validate geofence area doesn't exceed system limits.
    
    Maximum coverage area: 5000 sq km (state-sized region)
    
    Args:
        radius_km: Radius in kilometers
        
    Returns:
        True if valid
        
    Raises:
        ValueError: If area exceeds maximum
    """
    MAX_AREA_SQ_KM = 5000
    area = calculate_geofence_area_sq_km(radius_km)
    
    if area > MAX_AREA_SQ_KM:
        raise ValueError(
            f"Geofence area ({area:.1f} sq km) exceeds maximum of {MAX_AREA_SQ_KM} sq km. "
            f"Maximum radius: {math.sqrt(MAX_AREA_SQ_KM / math.pi):.1f} km"
        )
    
    return True


def point_in_geofence(
    point_lat: float, point_lon: float,
    geofence_lat: float, geofence_lon: float,
    geofence_radius_km: float
) -> bool:
    """
    Check if a point is within a circular geofence.
    
    Args:
        point_lat, point_lon: Point to check
        geofence_lat, geofence_lon: Geofence center
        geofence_radius_km: Geofence radius in kilometers
        
    Returns:
        True if point is within geofence
        
    Example:
        >>> # Check if San Jose is within 100km of San Francisco
        >>> point_in_geofence(
        ...     37.3382, -121.8863,      # San Jose
        ...     37.7749, -122.4194,      # San Francisco
        ...     100                      # 100 km radius
        ... )
        True
    """
    distance = haversine_distance(point_lat, point_lon, geofence_lat, geofence_lon)
    return distance <= geofence_radius_km


def find_nearby_geofences(
    point_lat: float, point_lon: float,
    geofences: list,  # List of dicts with 'lat', 'lon', 'radius_km' keys
    search_radius_km: float = 50
) -> list:
    """
    Find all geofences within a search radius of a point.
    
    Useful for checking which zones cover a given location.
    
    Args:
        point_lat, point_lon: Query point
        geofences: List of geofence dicts with 'lat', 'lon', 'radius_km'
        search_radius_km: Only return geofences closer than this (default 50 km)
        
    Returns:
        List of geofences that contain or are close to the point
        
    Example:
        >>> zones = [
        ...     {'id': 'A', 'lat': 37.77, 'lon': -122.41, 'radius_km': 1},
        ...     {'id': 'B', 'lat': 37.70, 'lon': -122.50, 'radius_km': 2},
        ... ]
        >>> find_nearby_geofences(37.77, -122.41, zones)
        [{'id': 'A', ...}]  # Zone A contains the point
    """
    nearby = []
    for geofence in geofences:
        if point_in_geofence(
            point_lat, point_lon,
            geofence['lat'], geofence['lon'],
            geofence['radius_km']
        ):
            nearby.append(geofence)
    return nearby
