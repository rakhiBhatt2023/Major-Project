

"""
===============================================================================
SMART GREEN PARKING SYSTEM — OBJECTIVE 2
RAMANDEEP KAUR'S MODULE: GREEN PARKING SCORE + RANKING LOGIC
===============================================================================
This module handles Ramandeep's assigned work for Objective 2:
  1. Design and code the Green Parking Score function: score = f(distance, congestion)
  2. Implement ranking logic to select the best-scoring vacant slot
  3. Tune/normalize weights for distance vs. congestion
  4. Validate against a first-available-slot baseline
 
INPUT:  the live slot feed produced by Shubam's RealtimeParkingTracker
        (realtime_tracker.py -> get_live_slot_feed())
OUTPUT: a single recommended slot dict, ready for Rakhi's Flask endpoint
        and SQLite logging table -> {slot_id, score, distance, congestion, timestamp}
===============================================================================
"""
 
from datetime import datetime
 
# ---------------------------------------------------------------------------
# CONFIG — normalization bounds and weights
# ---------------------------------------------------------------------------
# Based on Shubam's fixed distance map (5.0m - 10.0m) and adjacency network
# (max neighbor count per slot in the 4-slot 2x2 layout is 2).
MAX_DISTANCE = 10.0
MAX_CONGESTION = 2
 
# Weight split between distance and congestion (must sum to 1.0).
# Distance weighted slightly higher: a nearby-but-busy slot is still often
# preferable to a far-but-empty one, but not by a huge margin.
WEIGHT_DISTANCE = 0.6
WEIGHT_CONGESTION = 0.4
 
 
# ---------------------------------------------------------------------------
# STEP 1: THE SCORE FUNCTION — score = f(distance, congestion)
# ---------------------------------------------------------------------------
def compute_score(distance: float, congestion: int) -> float:
    """
    Lower score = better slot (closer + less congested).
 
    Both inputs are normalized to a 0-1 range before combining, so distance
    (measured in meters) and congestion (measured in occupied-neighbor count)
    contribute on the same scale regardless of their raw units.
    """
    norm_distance = min(distance / MAX_DISTANCE, 1.0)
    norm_congestion = min(congestion / MAX_CONGESTION, 1.0)
 
    score = (WEIGHT_DISTANCE * norm_distance) + (WEIGHT_CONGESTION * norm_congestion)
    return round(score, 4)
 
 
# ---------------------------------------------------------------------------
# STEP 2: RANKING LOGIC — pick the best-scoring vacant slot
# ---------------------------------------------------------------------------
def rank_vacant_slots(feed: dict) -> list:
    """
    Takes Shubam's live feed dict and returns a list of vacant slots sorted
    best-to-worst by score. Each entry keeps the original slot data plus
    its computed score.
    """
    vacant_slots = [s for s in feed.values() if s["vacant"]]
 
    for slot in vacant_slots:
        slot["score"] = compute_score(slot["distance"], slot["congestion"])
 
    ranked = sorted(vacant_slots, key=lambda s: s["score"])
    return ranked
 
 
def get_best_slot(feed: dict) -> dict:
    """Returns just the single top-ranked vacant slot, or None if none vacant."""
    ranked = rank_vacant_slots(feed)
    return ranked[0] if ranked else None
 
 
# ---------------------------------------------------------------------------
# STEP 3: BASELINE VALIDATION — compare against "first available slot"
# ---------------------------------------------------------------------------
def get_baseline_slot(feed: dict) -> dict:
    """
    Naive baseline: just the first vacant slot in feed order (A1 -> C3),
    with no scoring applied. Used to prove the scoring logic adds value.
    """
    for slot in feed.values():
        if slot["vacant"]:
            return slot
    return None
 
 
def compare_to_baseline(feed: dict) -> dict:
    """Returns both picks side by side for validation/reporting."""
    best = get_best_slot(feed)
    baseline = get_baseline_slot(feed)
 
    return {
        "scored_pick": {
            "slot_id": best["slot_id"],
            "distance": best["distance"],
            "congestion": best["congestion"],
            "score": best["score"],
        } if best else None,
        "baseline_pick": {
            "slot_id": baseline["slot_id"],
            "distance": baseline["distance"],
            "congestion": baseline["congestion"],
        } if baseline else None,
        "same_choice": (best["slot_id"] == baseline["slot_id"]) if (best and baseline) else None,
    }
 
 
# ---------------------------------------------------------------------------
# STEP 4: FINAL OUTPUT — packaged for Rakhi's API + SQLite log
# ---------------------------------------------------------------------------
def get_recommended_slot(feed: dict) -> dict:
    """
    This is the single function Rakhi's Flask endpoint calls.
    Returns a clean, ready-to-log recommendation dict.
    """
    best = get_best_slot(feed)
 
    if best is None:
        return {
            "slot_id": None,
            "score": None,
            "distance": None,
            "congestion": None,
            "timestamp": datetime.now().isoformat(),
            "message": "No vacant slots available"
        }
 
    return {
        "slot_id": best["slot_id"],
        "score": best["score"],
        "distance": best["distance"],
        "congestion": best["congestion"],
        "congestion_level": best["congestion_level"],
        "timestamp": datetime.now().isoformat(),
    }
 
 
# ---------------------------------------------------------------------------
# Debug / standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from realtime_tracker import RealtimeParkingTracker
 
    tracker = RealtimeParkingTracker()
    feed = tracker.get_live_slot_feed()
 
    print("\n------------------------------------------")
    print("RANKED VACANT SLOTS (best to worst)")
    print("------------------------------------------")
    for slot in rank_vacant_slots(feed):
        print(f"{slot['slot_id']} -> score={slot['score']} "
              f"(distance={slot['distance']}m, congestion={slot['congestion']})")
 
    print("\n------------------------------------------")
    print("RECOMMENDATION vs BASELINE")
    print("------------------------------------------")
    result = compare_to_baseline(feed)
    print(result)
 
    print("\n------------------------------------------")
    print("FINAL OUTPUT FOR RAKHI'S ENDPOINT")
    print("------------------------------------------")
    print(get_recommended_slot(feed))
 
