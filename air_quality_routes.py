"""
SmartPark AI
Objective 3 - Air Quality Flask Routes

Responsibility:
Rakhi

Provides API endpoints for the dashboard.
"""

from flask import Blueprint, jsonify

from air_quality_db import (
    get_latest_air_quality,
    get_recent_air_quality,
    get_air_quality_summary
)


air_quality_bp = Blueprint(
    "air_quality",
    __name__
)


from datetime import datetime

@air_quality_bp.route(
    "/api/air-quality/latest",
    methods=["GET"]
)
def latest_air_quality():

    reading = get_latest_air_quality()

    if reading is None:
        try:
            from app import _demo_mode
            if _demo_mode:
                return jsonify({
                    "success": True,
                    "data": {
                        "id": 0,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "sensor": "MQ135",
                        "raw_value": 285,
                        "alert_level": "SAFE",
                        "exceedance": 0,
                        "message": "MQ135 reading 285 is within safe range (Demo)."
                    }
                })
        except Exception:
            pass

        return jsonify({
            "success": False,
            "message": "No air-quality readings available."
        }), 404

    return jsonify({
        "success": True,
        "data": reading
    })


@air_quality_bp.route(
    "/api/air-quality/recent",
    methods=["GET"]
)
def recent_air_quality():

    readings = get_recent_air_quality(20)

    return jsonify({
        "success": True,
        "count": len(readings),
        "data": readings
    })


@air_quality_bp.route(
    "/api/air-quality/summary",
    methods=["GET"]
)
def air_quality_summary():

    summary = get_air_quality_summary()

    return jsonify({
        "success": True,
        "data": summary
    })