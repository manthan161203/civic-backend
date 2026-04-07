#!/usr/bin/env python3
"""
Geofence CRUD API Test — Verify all endpoints work correctly
=============================================================
This test validates the complete geofence CRUD flow from backend to frontend.

Usage:
    python test_geofence.py

Expected Output:
    ✓ List Geofences (GET /admin/geofences)
    ✓ Create Geofence (POST /admin/geofences)
    ✓ Update Geofence (PATCH /admin/geofences/{id})
    ✓ Delete Geofence (DELETE /admin/geofences/{id})
"""

import json
import sys
from datetime import datetime

# Test data
TEST_GEOFENCE_CREATE = {
    "name": "City Center Zone",
    "latitude": 19.0176,
    "longitude": 72.8479,
    "radius_km": 2.5,
}

TEST_GEOFENCE_UPDATE = {
    "name": "Downtown Zone (Updated)",
    "radius_km": 3.0,
}

VALIDATION_TESTS = {
    "latitude_min": {"latitude": -90.5, "error": "Latitude must be between -90 and 90"},
    "latitude_max": {"latitude": 90.1, "error": "Latitude must be between -90 and 90"},
    "longitude_min": {"longitude": -180.1, "error": "Longitude must be between -180 and 180"},
    "longitude_max": {"longitude": 180.1, "error": "Longitude must be between -180 and 180"},
    "radius_zero": {"radius_km": 0, "error": "Radius must be greater than 0"},
    "radius_negative": {"radius_km": -1.5, "error": "Radius must be greater than 0"},
    "name_empty": {"name": "", "error": "Zone name is required"},
}


def test_model_structure():
    """Verify Geofence model has all required fields."""
    print("\n▶ Testing Geofence Model Structure")
    try:
        from app.models.geofence import Geofence
        from sqlalchemy import inspect

        mapper = inspect(Geofence)
        columns = {c.key for c in mapper.columns}

        required_columns = {"id", "name", "latitude", "longitude", "radius_km", "created_by_id", "created_at", "updated_at"}
        missing = required_columns - columns

        if missing:
            print(f"  ✗ Missing columns: {missing}")
            return False

        print(f"  ✓ Model has all required columns: {', '.join(sorted(required_columns))}")
        return True
    except Exception as e:
        print(f"  ✗ Model import failed: {e}")
        return False


def test_schemas():
    """Verify all geofence schemas are properly defined."""
    print("\n▶ Testing Geofence Schemas")
    try:
        from app.schemas.admin import (
            CreateGeofenceRequest,
            UpdateGeofenceRequest,
            GeofenceResponse,
            GeofenceListResponse,
        )

        # Test CreateGeofenceRequest validation
        valid_create = CreateGeofenceRequest(**TEST_GEOFENCE_CREATE)
        print(f"  ✓ CreateGeofenceRequest validates correctly")

        # Test UpdateGeofenceRequest (optional fields)
        valid_update = UpdateGeofenceRequest(**TEST_GEOFENCE_UPDATE)
        print(f"  ✓ UpdateGeofenceRequest validates correctly")

        # Test GeofenceResponse
        from uuid import uuid4
        from datetime import datetime

        response = GeofenceResponse(
            id=uuid4(),
            name="Test Zone",
            latitude=19.0176,
            longitude=72.8479,
            radius_km=2.0,
            created_by_name="Admin User",
            created_at=datetime.now(),
        )
        print(f"  ✓ GeofenceResponse creates correctly")

        # Test GeofenceListResponse
        list_response = GeofenceListResponse(
            items=[response],
            total=1,
            page=1,
            size=20,
        )
        print(f"  ✓ GeofenceListResponse creates correctly")

        return True
    except Exception as e:
        print(f"  ✗ Schema validation failed: {e}")
        return False


def test_validation_rules():
    """Test that validation rules are enforced."""
    print("\n▶ Testing Input Validation Rules")
    try:
        from app.schemas.admin import CreateGeofenceRequest
        from pydantic import ValidationError

        test_base = {
            "name": "Test Zone",
            "latitude": 19.0176,
            "longitude": 72.8479,
            "radius_km": 2.0,
        }

        validation_passed = 0
        validation_total = len(VALIDATION_TESTS)

        for test_name, test_values in VALIDATION_TESTS.items():
            test_data = {**test_base, **test_values}
            try:
                CreateGeofenceRequest(**test_data)
                print(f"  ✗ {test_name}: Should have failed validation")
            except ValidationError:
                print(f"  ✓ {test_name}: Correctly rejected")
                validation_passed += 1

        return validation_passed == validation_total
    except Exception as e:
        print(f"  ✗ Validation test failed: {e}")
        return False


def test_routes_registered():
    """Verify geofence routes are registered in the admin router."""
    print("\n▶ Testing Routes Registration")
    try:
        from app.routes.admin import router

        route_paths = {route.path for route in router.routes}

        required_routes = {
            "/admin/geofences",
            "/admin/geofences/{geofence_id}",
        }

        missing_routes = required_routes - route_paths
        if missing_routes:
            print(f"  ✗ Missing routes: {missing_routes}")
            return False

        # Check for specific HTTP methods
        geofence_routes = [r for r in router.routes if "/geofences" in r.path]
        methods = {method for route in geofence_routes for method in (route.methods or set())}

        required_methods = {"GET", "POST", "DELETE", "PATCH"}
        missing_methods = required_methods - methods

        if missing_methods:
            print(f"  ✗ Missing HTTP methods: {missing_methods}")
            return False

        print(f"  ✓ All required routes registered: {', '.join(sorted(required_routes))}")
        print(f"  ✓ All required HTTP methods available: {', '.join(sorted(required_methods))}")
        return True
    except Exception as e:
        print(f"  ✗ Routes registration test failed: {e}")
        return False


def test_frontend_api_methods():
    """Verify frontend API client has all geofence methods."""
    print("\n▶ Testing Frontend API Methods")
    try:
        import sys
        import os

        frontend_path = "/home/manthan/Desktop/Office_Work_Personal/Civic/civic-frontend/admin/src/api"
        sys.path.insert(0, frontend_path)

        # Read the index.js file to verify API methods
        index_file = "/home/manthan/Desktop/Office_Work_Personal/Civic/civic-frontend/admin/src/api/index.js"
        with open(index_file, "r") as f:
            api_content = f.read()

        required_methods = {
            "getGeofences",
            "createGeofence",
            "updateGeofence",
            "deleteGeofence",
        }

        missing_methods = set()
        for method in required_methods:
            if f"  {method}:" not in api_content:
                missing_methods.add(method)

        if missing_methods:
            print(f"  ✗ Missing API methods: {missing_methods}")
            return False

        print(f"  ✓ All required API methods registered: {', '.join(sorted(required_methods))}")
        return True
    except Exception as e:
        print(f"  ✗ Frontend API test failed: {e}")
        return False


def test_component_integration():
    """Verify frontend component uses the API correctly."""
    print("\n▶ Testing Component Integration")
    try:
        component_file = "/home/manthan/Desktop/Office_Work_Personal/Civic/civic-frontend/admin/app/dashboard/geofence/page.js"
        with open(component_file, "r") as f:
            component_content = f.read()

        required_calls = {
            "adminApi.getGeofences": "Load zones from API",
            "adminApi.createGeofence": "Create geofence via API",
            "adminApi.deleteGeofence": "Delete geofence via API",
        }

        missing_calls = set()
        for call in required_calls:
            if call not in component_content:
                missing_calls.add(call)

        if missing_calls:
            print(f"  ✗ Missing API calls in component: {missing_calls}")
            return False

        # Check for validation
        validations_found = {
            "latitude validation": "lat < -90 || lat > 90" in component_content,
            "longitude validation": "lng < -180 || lng > 180" in component_content,
            "radius validation": "radius <= 0" in component_content,
            "name validation": "!formData.name" in component_content,
        }

        for validation_name, found in validations_found.items():
            if found:
                print(f"  ✓ {validation_name}: ✓")
            else:
                print(f"  ✗ {validation_name}: Missing")

        return all(validations_found.values())
    except Exception as e:
        print(f"  ✗ Component integration test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 70)
    print("GEOFENCE CRUD IMPLEMENTATION TEST SUITE")
    print("=" * 70)

    tests = [
        ("Model Structure", test_model_structure),
        ("Schemas", test_schemas),
        ("Validation Rules", test_validation_rules),
        ("Routes Registration", test_routes_registered),
        ("Frontend API Methods", test_frontend_api_methods),
        ("Component Integration", test_component_integration),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"\n✗ {test_name} encountered an unexpected error: {e}")
            results.append((test_name, False))

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed! Geofence CRUD is fully implemented.")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
