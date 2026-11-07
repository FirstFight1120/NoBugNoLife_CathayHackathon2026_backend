from fastapi import FastAPI, APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from collections import defaultdict
import math

from src.nbnl_backend.db import get_db
from src.nbnl_backend import models, schemas

app = FastAPI(title="Cathay Cargo API")
router = APIRouter()

# 1st API endpoint: Prevention Plans
@router.get("/api/v1/prevention-plans", response_model=schemas.PreventionPlanResponse)
def get_prevention_plans(page: int = 1, limit: int = 20, db: Session = Depends(get_db)):
    
    # 1. Query the new Ternary Entity (AwbUldFlight)
    # We only pull records that have been scored by the Neural Network
    risky_manifest_items = db.query(models.AwbUldFlight)\
        .options(
            joinedload(models.AwbUldFlight.awb),
            joinedload(models.AwbUldFlight.uld),
            joinedload(models.AwbUldFlight.flight_flight)
        )\
        .filter(models.AwbUldFlight.offload_probability.isnot(None))\
        .all()
    
    if not risky_manifest_items:
        return {
            "items": [], "total_cases": 0, "total_shipments_at_risk": 0, 
            "pagination": {"page": page, "total_pages": 1, "has_next_page": False}
        }

    # 2. Group by ULD + Flight (The physical unit to offload)
    uld_flights_data = defaultdict(lambda: {
        "flight_id": None,
        "target_uld_no": None,
        "at_risk_awbs": set(),
        "risk_probabilities": [], # Used for temporary averaging
        "max_risk_level": "Low",
        "risk_types": set(),
        "reasons": set(),
        "value_at_risk": 0.0
    })

    for item in risky_manifest_items:
        # Composite key: (ULD_NO, FLIGHT_ID)
        key = (item.uld_uld_no, item.flight_flight_id)
        
        d = uld_flights_data[key]
        d["flight_id"] = item.flight_flight_id
        d["target_uld_no"] = item.uld_uld_no
        d["at_risk_awbs"].add(item.awb_awb_no)
        
        # Collect probabilities for the average calculation
        prob = float(item.offload_probability) if item.offload_probability else 0.0
        d["risk_probabilities"].append(prob)
        
        # Aggregate financial value using the linked AWB object
        d["value_at_risk"] += float(item.awb.declared_value) if item.awb and item.awb.declared_value else 0.0
        
        # Keep track of the highest individual risk level for UI display
        if prob >= 0.80:
            d["max_risk_level"] = "Critical"
        elif prob >= 0.50 and d["max_risk_level"] != "Critical":
            d["max_risk_level"] = "Moderate"
            
        # Merge risk types and reasons from the AwbUldFlight record
        if item.risk_types:
            d["risk_types"].update(item.risk_types)
        if item.risk_reasons:
            d["reasons"].update(item.risk_reasons)

    # 3. Calculate Averages and Apply the 80% Threshold
    all_items = []
    total_shipments_at_risk = 0

    for key, data in uld_flights_data.items():
        # Calculate average risk of the ULD (Temporary logic until weights are assigned)
        avg_risk = sum(data["risk_probabilities"]) / len(data["risk_probabilities"])
        
        # If the average is >= 80%, flag for offload
        if avg_risk >= 0.80:
            total_shipments_at_risk += len(data["at_risk_awbs"])
            
            item = schemas.PreventionPlanItem(
                flight_id=data["flight_id"],
                target_uld_no=data["target_uld_no"],
                at_risk_awbs=list(data["at_risk_awbs"]),
                status="PREVENTIVE_ACTION_REQUIRED",
                risk_level=data["max_risk_level"],
                risk_types=list(data["risk_types"]),
                offload_probability=avg_risk, # Use the AVERAGE score for the ULD
                predicted_offload_rate=0.04,  # Mocked: can be calculated from NN if needed
                affected_shipment_count=len(data["at_risk_awbs"]),
                forecast_risk=schemas.ForecastRisk(
                    offload_probability=avg_risk,
                    capacity_shortfall_kg=600.0, # Mocked: Can be pulled from Flight or NN
                    reasons=list(data["reasons"])
                ),
                value_at_risk=data["value_at_risk"]
            )
            all_items.append(item)

    # 4. Pagination
    total_cases = len(all_items)
    total_pages = math.ceil(total_cases / limit) if total_cases > 0 else 1
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit

    return schemas.PreventionPlanResponse(
        items=all_items[start_idx:end_idx],
        total_cases=total_cases,
        total_shipments_at_risk=total_shipments_at_risk,
        pagination=schemas.Pagination(
            page=page,
            total_pages=total_pages,
            has_next_page=page < total_pages
        )
    )


# 2nd API endpoint: Flight Details
@router.get("/api/v1/prevention-plans/{flight_id}", response_model=schemas.FlightDetailsResponse)
def get_flight_details(flight_id: str, db: Session = Depends(get_db)):
    # 1. Fetch the Flight and Aircraft details
    flight = db.query(models.Flight).options(
        joinedload(models.Flight.aircraft_aircraft)
    ).filter(models.Flight.flight_id == flight_id).first()

    if not flight:
        raise HTTPException(status_code=404, detail="Flight not found")

    # 2. Fetch all AWB_ULD_FLIGHT records for this flight
    manifest_items = db.query(models.AwbUldFlight).options(
        joinedload(models.AwbUldFlight.awb).joinedload(models.Awb.shipment),
        joinedload(models.AwbUldFlight.uld)
    ).filter(models.AwbUldFlight.flight_flight_id == flight_id).all()

    # 3. Calculate Capacity Metrics
    usable_capacity = float(flight.usable_capacity_kg) if flight.usable_capacity_kg else float(flight.aircraft_aircraft.max_payload_weight or 0.0)
    booked_weight = sum(float(item.weight) for item in manifest_items if item.weight)
    booked_volume = sum(float(item.volume) for item in manifest_items if item.volume)
    remaining_capacity = usable_capacity - booked_weight

    # 4. Build Affected Shipments List & Determine Overall Risk
    affected_shipments = []
    max_risk_prob = 0.0
    overall_risk_level = "Low"
    primary_reason = ""
    forecast_timestamp = None
    forecast_confidence = 0.0
    
    for item in manifest_items:
        if item.offload_probability:
            prob = float(item.offload_probability)
            if prob > max_risk_prob:
                max_risk_prob = prob
                overall_risk_level = item.risk_level or "Moderate"
                forecast_timestamp = item.forecast_timestamp
                forecast_confidence = float(item.forecast_confidence) if item.forecast_confidence else 0.0

                if item.risk_reasons and isinstance(item.risk_reasons, list):
                    if len(item.risk_reasons) > 0:
                        primary_reason = str(item.risk_reasons[0])


        for shipment in item.awb.shipment:
            customer_name = shipment.shipper.comp_name if shipment.shipper else "Unknown"
            
            priority_enum = "Standard"
            if item.awb.priority_score:
                if float(item.awb.priority_score) >= 90:
                    priority_enum = "Critical"
                elif float(item.awb.priority_score) >= 70:
                    priority_enum = "High"

            # Note: Using snake_case here. Pydantic will convert to camelCase in the response.
            affected_shipments.append({
                "shipment_id": shipment.s_no,
                "customer": customer_name,
                "origin": item.awb.origin,
                "destination": item.awb.dest,
                "weight_kg": float(item.weight) if item.weight else 0.0,
                "volume_m3": float(item.volume) if item.volume else 0.0,
                "priority": priority_enum,
                "delivery_deadline": item.awb.contract_ddl.isoformat() + "Z" if item.awb.contract_ddl else None,
                "offload_probability": float(item.offload_probability) if item.offload_probability else 0.0,
                "value_at_risk": float(item.awb.declared_value) if item.awb.declared_value else 0.0
            })

    status = "PREVENTIVE_ACTION_REQUIRED" if max_risk_prob >= 0.50 else "AT_RISK"
    if max_risk_prob == 0.0:
        status = "REVIEWED"

    # 5. Construct the final response (Using snake_case keys)
    return {
        "plan_id": f"PP-{flight.flight_no}-{flight.std.strftime('%Y%m%d') if flight.std else '20261016'}",
        "currency": "HKD",
        "flight": {
            "flight_id": flight.flight_no,
            "origin": flight.origin,
            "destination": flight.dest,
            "scheduled_departure": flight.std.isoformat() + "Z" if flight.std else None,
            "aircraft_type": flight.aircraft_aircraft.aircraft_type
        },
        "review": {
            "status": status,
            "risk_level": overall_risk_level,
            "reason": primary_reason or "No current risk detected",
            "forecast_timestamp": forecast_timestamp.isoformat() + "Z" if forecast_timestamp else "2026-10-16T09:10:00Z",
            "forecast_confidence": forecast_confidence
        },
        "capacity": {
            "usable_capacity_kg": usable_capacity,
            "booked_weight_kg": booked_weight,
            "remaining_capacity_kg": remaining_capacity,
            "booked_volume_m3": booked_volume
        },
        "affected_shipments": affected_shipments,
        "assumptions": [
            "Approved payload is based on operational capacity.",
            "Weather impact remains forecast evidence.",
            "Partner capacity requires operational confirmation.",
            "Connecting flights are tracked as independent flight records."
        ]
    }

app.include_router(router)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "API is running"}