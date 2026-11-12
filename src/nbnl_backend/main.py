from contextlib import asynccontextmanager
from datetime import datetime
from sqlalchemy.orm import Session, joinedload
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional
from fastapi import FastAPI, APIRouter, Depends, HTTPException


from src.nbnl_backend.db import get_db
from src.nbnl_backend import models, schemas
from src.nbnl_backend.bg_tasks_pri_risk import run_batch_update_job
from src.nbnl_backend.bg_tasks_scanner import run_disruption_scanner


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    
    # 1. Priority & Risk ML Calculator (Runs every 60 minutes)
    scheduler.add_job(
        run_batch_update_job, 
        'interval', 
        minutes=60, 
        next_run_time=datetime.now() # Run immediately on startup
    )
    
    # 2. Disruption Scanner (Runs every 5 minutes)
    scheduler.add_job(
        run_disruption_scanner, 
        'interval', 
        minutes=5, 
        next_run_time=datetime.now() # Run immediately on startup
    )
    
    scheduler.start()
    print("✅ Background job schedulers started (ML & Scanner).")
    yield
    scheduler.shutdown()
    print("❌ Background job schedulers stopped.")

app = FastAPI(title="Cathay Cargo API", lifespan=lifespan)
router = APIRouter()

# Helper function to build the Workflow Control (Gating Logic)
def get_workflow_control(db: Session) -> dict:
    active_disruptions = db.query(models.Offload).filter(
        models.Offload.source_type == 'Disrupted', 
        models.Offload.status != 'Resolved'
    ).all()
    
    active_predictions = db.query(models.AwbUldFlight).filter(
        models.AwbUldFlight.offload_probability >= 0.80,
        models.AwbUldFlight.status != 'Offloaded'
    ).all()

    active_pool = db.query(models.Offload).filter(
        models.Offload.source_type == 'Displaced',
        models.Offload.status != 'Resolved'
    ).all()

    active_priority = None
    blocking_ids = []
    
    if active_disruptions:
        active_priority = 1
        blocking_ids = [str(d.offload_id) for d in active_disruptions]
    elif active_predictions:
        active_priority = 2
        blocking_ids = [f"{p.awb_awb_no}-{p.uld_uld_no}" for p in active_predictions]
    elif active_pool:
        active_priority = 3

    return {
        "active_priority": active_priority,
        "can_work_disruptions": True,
        "can_work_predictions": active_priority in [None, 2, 3],
        "can_review_pool": active_priority in [None, 3],
        "allowed_actions_by_type": {
            "DISRUPTION": ["APPROVE_FOR_REVIEW", "REJECT_OVERRIDE"] if active_priority == 1 else [],
            "PREDICTED_OFFLOAD": ["APPROVE_FOR_REVIEW", "REJECT_OVERRIDE"] if active_priority == 2 else [],
            "PENDING_POOL": ["HOLD_FOR_PRIORITY", "CONSIDER_FOR_NO_SHOW"] if active_priority in [1, 2] else ["HOLD_FOR_PRIORITY", "RELEASE_HOLD", "CONSIDER_FOR_NO_SHOW", "CLEAR_NO_SHOW_REVIEW"]
        },
        "blocking_work_item_ids": blocking_ids,
        "message": "Resolve active disruption work before advancing." if active_priority == 1 else None
    }

@router.get("/api/v1/recovery-solutions", response_model=schemas.RecoverySolutionListResponse)
def list_recovery_solutions(
    type: Optional[str] = None, 
    page: int = 1, 
    limit: int = 20, 
    db: Session = Depends(get_db)
):
    workflow_control = get_workflow_control(db)
    items = []

    if type in [None, "DISRUPTION"]:
        disruptions = db.query(models.Offload).options(
            joinedload(models.Offload.uld).joinedload(models.Uld.loadtype_loadtype),
            joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft)
        ).filter(models.Offload.source_type == 'Disrupted', models.Offload.status != 'Resolved').all()
        
        for d in disruptions:
            items.append(_map_offload_to_work_item(d, "DISRUPTION"))

    if type in [None, "PREDICTED_OFFLOAD"]:
        predictions = db.query(models.AwbUldFlight).options(
            joinedload(models.AwbUldFlight.awb),
            joinedload(models.AwbUldFlight.uld),
            joinedload(models.AwbUldFlight.flight_flight).joinedload(models.Flight.aircraft_aircraft)
        ).filter(models.AwbUldFlight.offload_probability >= 0.80, models.AwbUldFlight.status != 'Offloaded').all()
        
        for p in predictions:
            items.append(_map_prediction_to_work_item(p))

    total_items = len(items)
    start_idx = (page - 1) * limit
    paginated_items = items[start_idx:start_idx + limit]

    return {
        "items": paginated_items,
        "total_items": total_items,
        "total_by_type": {"DISRUPTION": 1, "PREDICTED_OFFLOAD": 2, "PENDING_POOL": 0},
        "pagination": {"page": page, "total_pages": 1, "has_next_page": False},
        "workflow_control": workflow_control,
        "flight_summaries": []
    }

@router.post("/api/v1/recovery-solutions/{work_item_id}/decisions", status_code=201)
def record_decision(work_item_id: str, payload: schemas.DecisionRequest, db: Session = Depends(get_db)):
    wf = get_workflow_control(db)
    if payload.type == "PREDICTED_OFFLOAD" and wf["active_priority"] == 1:
        raise HTTPException(status_code=423, detail={"code": "HIGHER_PRIORITY_WORK_OPEN", "blockingWorkItemIds": wf["blocking_item_ids"]})
    
    if payload.type == "PENDING_POOL" and wf["active_priority"] in [1, 2]:
        if payload.decision != "HOLD_FOR_PRIORITY":
             raise HTTPException(status_code=423, detail={"code": "HIGHER_PRIORITY_WORK_OPEN"})

    awb = db.query(models.Awb).filter(models.Awb.awb_no == payload.master_airway_bill_number).first()
    if awb and (awb.document_version or 1) != payload.document_version:
        raise HTTPException(status_code=409, detail="Document version mismatch. Please refresh.")

    new_decision = models.RecoveryDecision(
        work_item_id=work_item_id,
        work_item_type=payload.type,
        uld_id=payload.uld_id,
        awb_no=payload.master_airway_bill_number,
        document_version=payload.document_version,
        option_id=payload.option_id,
        decision=payload.decision,
        reason=payload.reason,
        reviewer_id=payload.reviewer_id,
        reviewed_at=datetime.fromisoformat(payload.reviewed_at.replace("Z", "+00:00"))
    )
    db.add(new_decision)
    
    if awb:
        # FIX: Handle None for document_version
        awb.document_version = (awb.document_version or 1) + 1
        
    db.commit()
    return {"decisionId": new_decision.decision_id, "workItemId": work_item_id, "message": "Decision saved."}


def _map_offload_to_work_item(offload: models.Offload, item_type: str):
    manifest = offload.uld.awb_uld_flight[0] if offload.uld and offload.uld.awb_uld_flight else None
    awb = manifest.awb if manifest else None
    
    return schemas.WorkItem(
        work_item_id=str(offload.offload_id),
        type=item_type,
        status=offload.status or "PENDING", # FIX
        uld=schemas.ULDInfo(
            uld_id=offload.uld.uld_no,
            uld_type=offload.uld.loadtype_loadtype.l_type or "UNK" if offload.uld and offload.uld.loadtype_loadtype else "UNK", # FIX
            weight_kg=float(manifest.weight or 0) if manifest else 0.0, # FIX
            volume_m3=float(manifest.volume or 0) if manifest else 0.0, # FIX
            origin=offload.flight.origin or "UNK", # FIX
            destination=offload.flight.dest or "UNK" # FIX
        ),
        master_airway_bill=schemas.MAWBInfo(
            number=awb.awb_no if awb else "UNKNOWN",
            document_id=f"MAWB-DOC-{awb.awb_no}" if awb else "UNKNOWN",
            display_label=f"MAWB {awb.awb_no}" if awb else "UNKNOWN"
        ),
        flight=schemas.FlightInfo(
            flight_id=offload.flight.flight_id,
            origin=offload.flight.origin or "UNK", # FIX
            destination=offload.flight.dest or "UNK", # FIX
            scheduled_departure=offload.flight.std.isoformat() + "Z" if offload.flight.std else "",
            aircraft_type=offload.flight.aircraft_aircraft.aircraft_type if offload.flight.aircraft_aircraft else None
        ),
        cargo=schemas.CargoInfo(
            shipment_ids=[s.s_no for s in awb.shipment] if awb else [],
            weight_kg=float(manifest.weight or 0) if manifest else 0.0, # FIX
            volume_m3=float(manifest.volume or 0) if manifest else 0.0, # FIX
            handling_requirements=["GENERAL_CARGO"],
            commodity=None,
            value_at_risk=float(awb.declared_value or 0) if awb else 0.0, # FIX
            currency="HKD"
        ),
        document=schemas.DocumentInfo(
            document_id=f"MAWB-DOC-{awb.awb_no}" if awb else "UNKNOWN",
            version=awb.document_version or 1 if awb else 1, # FIX
            updated_at=datetime.now().isoformat() + "Z" # FIX (datetime module)
        ),
        updated_at=datetime.now().isoformat() + "Z" # FIX
    )

def _map_prediction_to_work_item(pred: models.AwbUldFlight):
    # FIX: Safely cast JSONB to list
    safe_reasons = pred.risk_reasons if isinstance(pred.risk_reasons, list) else []
    
    return schemas.WorkItem(
        work_item_id=f"{pred.awb_awb_no}-{pred.uld_uld_no}",
        type="PREDICTED_OFFLOAD",
        status="PREVENTIVE_ACTION_REQUIRED" if float(pred.offload_probability or 0) >= 0.90 else "AT_RISK", # FIX
        uld=schemas.ULDInfo(
            uld_id=pred.uld_uld_no,
            uld_type=pred.uld.loadtype_loadtype.l_type or "UNK" if pred.uld and pred.uld.loadtype_loadtype else "UNK", # FIX
            weight_kg=float(pred.weight or 0), # FIX
            volume_m3=float(pred.volume or 0), # FIX
            origin=pred.awb.origin or "UNK", # FIX
            destination=pred.awb.dest or "UNK" # FIX
        ),
        master_airway_bill=schemas.MAWBInfo(
            number=pred.awb_awb_no,
            document_id=f"MAWB-DOC-{pred.awb_awb_no}",
            display_label=f"MAWB {pred.awb_awb_no}"
        ),
        flight=schemas.FlightInfo(
            flight_id=pred.flight_flight.flight_id,
            origin=pred.flight_flight.origin or "UNK", # FIX
            destination=pred.flight_flight.dest or "UNK", # FIX
            scheduled_departure=pred.flight_flight.std.isoformat() + "Z" if pred.flight_flight.std else "",
            aircraft_type=pred.flight_flight.aircraft_aircraft.aircraft_type if pred.flight_flight.aircraft_aircraft else None
        ),
        cargo=schemas.CargoInfo(
            shipment_ids=[s.s_no for s in pred.awb.shipment],
            weight_kg=float(pred.weight or 0), # FIX
            volume_m3=float(pred.volume or 0), # FIX
            handling_requirements=["GENERAL_CARGO"],
            commodity=None,
            value_at_risk=float(pred.awb.declared_value or 0), # FIX
            currency="HKD"
        ),
        forecast=schemas.ForecastInfo(
            offload_probability=float(pred.offload_probability or 0), # FIX
            capacity_shortfall_kg=600.0,
            confidence=int(float(pred.forecast_confidence or 0) * 100), # FIX
            forecasted_at=pred.forecast_timestamp.isoformat() + "Z" if pred.forecast_timestamp else datetime.now().isoformat() + "Z", # FIX
            reasons=safe_reasons, # FIX
            forecast_cargo_capacity_kg=12000.0,
            booked_weight_kg=12600.0,
            value_at_risk=float(pred.awb.declared_value or 0), # FIX
            currency="HKD"
        ),
        document=schemas.DocumentInfo(
            document_id=f"MAWB-DOC-{pred.awb_awb_no}",
            version=pred.awb.document_version or 1 if pred.awb else 1, # FIX
            updated_at=datetime.now().isoformat() + "Z" # FIX
        ),
        updated_at=datetime.now().isoformat() + "Z" # FIX
    )

app.include_router(router)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "API is running"}