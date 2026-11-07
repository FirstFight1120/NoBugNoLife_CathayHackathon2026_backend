from fastapi import FastAPI, APIRouter, Depends
from sqlalchemy.orm import Session, joinedload
from collections import defaultdict
import math

from src.nbnl_backend.db import get_db
from src.nbnl_backend import models, schemas

app = FastAPI(title="Cathay Cargo API")
router = APIRouter()

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

app.include_router(router)