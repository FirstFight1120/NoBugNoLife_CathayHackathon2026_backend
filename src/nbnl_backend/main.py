from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session, joinedload
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional
from fastapi import FastAPI, APIRouter, Query, Depends, HTTPException, status, Response
import math


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
        next_run_time=datetime.now(timezone.utc) # Run immediately on startup
    )
    
    # 2. Disruption Scanner (Runs every 5 minutes)
    scheduler.add_job(
        run_disruption_scanner, 
        'interval', 
        minutes=5, 
        next_run_time=datetime.now(timezone.utc) # Run immediately on startup
    )
    
    scheduler.start()
    print("Background job schedulers started (ML & Scanner).")
    yield
    scheduler.shutdown()
    print("Background job schedulers stopped.")

app = FastAPI(title="Cathay Cargo API", lifespan=lifespan)
router = APIRouter()

# Helper function to build the Workflow Control (Gating Logic)
# --- 1. WORKFLOW CONTROL (GATING LOGIC) ---
def get_workflow_control(db: Session) -> dict:
    # Terminal statuses for Disruptions: RECOVERED, CLOSED
    active_disruptions = db.query(models.Offload).filter(
        models.Offload.source_type == 'Disrupted', 
        models.Offload.status.notin_(['RECOVERED', 'CLOSED'])
    ).count()
    
    # Terminal statuses for Predictions: CLEARED
    active_predictions = db.query(models.AwbUldFlight).filter(
        models.AwbUldFlight.offload_probability >= 0.80,
        models.AwbUldFlight.status != 'CLEARED'
    ).count()

    # Terminal statuses for Pool: RETIRED
    active_pool = db.query(models.Offload).filter(
        models.Offload.source_type == 'Displaced',
        models.Offload.status != 'RETIRED'
    ).count()

    active_priority = None
    blocking_ids = []
    
    if active_disruptions > 0:
        active_priority = 1
        blocking = db.query(models.Offload).filter(
            models.Offload.source_type == 'Disrupted', 
            models.Offload.status.notin_(['RECOVERED', 'CLOSED'])
        ).limit(3).all()
        blocking_ids = [str(b.offload_id) for b in blocking]
    elif active_predictions > 0:
        active_priority = 2
        blocking = db.query(models.AwbUldFlight).filter(
            models.AwbUldFlight.offload_probability >= 0.80,
            models.AwbUldFlight.status != 'CLEARED'
        ).limit(3).all()
        blocking_ids = [f"{b.awb_awb_no}-{b.uld_uld_no}" for b in blocking]
    elif active_pool > 0:
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

# --- ENDPOINT 1: List Recovery Solutions ---
@router.get("/api/v1/recovery-solutions", response_model=schemas.RecoverySolutionListResponse)
def list_recovery_solutions(
    type: Optional[str] = Query(None, pattern="^(DISRUPTION|PREDICTED_OFFLOAD|PENDING_POOL)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    master_airway_bill_number: Optional[str] = None,
    flight_id: Optional[str] = None,
    status: Optional[str] = None,
    include_closed: bool = False,
    db: Session = Depends(get_db)
):
    workflow_control = get_workflow_control(db)
    items = []

    # --- BASE QUERIES & COUNTS (Independent of `type` filter) ---
    q_disruptions = db.query(models.Offload).filter(models.Offload.source_type == 'Disrupted')
    if not include_closed: q_disruptions = q_disruptions.filter(models.Offload.status.notin_(['RECOVERED', 'CLOSED', 'Resolved']))
    if flight_id: q_disruptions = q_disruptions.filter(models.Offload.flight_id == flight_id)
    if status: q_disruptions = q_disruptions.filter(models.Offload.status == status)
    total_disruptions = q_disruptions.count()

    q_predictions = db.query(models.AwbUldFlight).filter(models.AwbUldFlight.offload_probability >= 0.80)
    if not include_closed: q_predictions = q_predictions.filter(models.AwbUldFlight.status != 'CLEARED')
    if flight_id: q_predictions = q_predictions.filter(models.AwbUldFlight.flight_flight_id == flight_id)
    if master_airway_bill_number: q_predictions = q_predictions.filter(models.AwbUldFlight.awb_awb_no == master_airway_bill_number)
    total_predictions = q_predictions.count()

    q_pool = db.query(models.Offload).filter(models.Offload.source_type == 'Displaced')
    if not include_closed: q_pool = q_pool.filter(models.Offload.status != 'RETIRED')
    if flight_id: q_pool = q_pool.filter(models.Offload.flight_id == flight_id)
    if status: q_pool = q_pool.filter(models.Offload.status == status)
    total_pool = q_pool.count()

    # --- FETCH DATA BASED ON TYPE ---
    if type in [None, "DISRUPTION"]:
        disruptions = q_disruptions.options(
            joinedload(models.Offload.uld).joinedload(models.Uld.loadtype_loadtype),
            joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft)
        ).all()
        for d in disruptions:
            items.append(_map_offload_to_work_item(d, "DISRUPTION"))

    if type in [None, "PREDICTED_OFFLOAD"]:
        predictions = q_predictions.options(
            joinedload(models.AwbUldFlight.awb).joinedload(models.Awb.shipment),
            joinedload(models.AwbUldFlight.uld).joinedload(models.Uld.loadtype_loadtype),
            joinedload(models.AwbUldFlight.flight_flight).joinedload(models.Flight.aircraft_aircraft)
        ).all()
        for p in predictions:
            items.append(_map_prediction_to_work_item(p))

    if type in [None, "PENDING_POOL"]:
        pool_items = q_pool.options(
            joinedload(models.Offload.uld).joinedload(models.Uld.loadtype_loadtype),
            joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft)
        ).all()
        for p in pool_items:
            items.append(_map_offload_to_work_item(p, "PENDING_POOL"))

    # --- PAGINATION ---
    total_items = len(items)
    start_idx = (page - 1) * limit
    paginated_items = items[start_idx:start_idx + limit]
    total_pages = math.ceil(total_items / limit) if total_items > 0 else 1

    # --- FLIGHT SUMMARIES ---
    flight_summaries = []
    if type in [None, "PREDICTED_OFFLOAD"]:
        # Aggregate ULDs by Flight
        flight_map = {}
        for p in predictions:
            fid = p.flight_flight_id
            if fid not in flight_map:
                flight_map[fid] = {
                    "flightId": fid,
                    "origin": p.flight_flight.origin or "UNK",
                    "destination": p.flight_flight.dest or "UNK",
                    "scheduledDeparture": p.flight_flight.std.isoformat() + "Z" if p.flight_flight.std else "",
                    "aircraftType": p.flight_flight.aircraft_aircraft.aircraft_type if p.flight_flight.aircraft_aircraft else None,
                    "offloadProbability": 0.0,
                    "capacityShortfallKg": 600.0, # Mocked, should come from Flight model
                    "forecastCargoCapacityKg": 12000.0, # Mocked
                    "bookedWeightKg": 12600.0, # Mocked
                    "affectedUldCount": 0,
                    "shipmentIds": set(),
                    "valueAtRisk": 0.0,
                    "currency": "HKD",
                    "confidence": 0,
                    "forecastedAt": p.forecast_timestamp.isoformat() + "Z" if p.forecast_timestamp else "",
                    "reasons": []
                }
            
            f_sum = flight_map[fid]
            f_sum["affectedUldCount"] += 1
            f_sum["offloadProbability"] = max(f_sum["offloadProbability"], float(p.offload_probability or 0))
            f_sum["confidence"] = max(f_sum["confidence"], int(float(p.forecast_confidence or 0) * 100))
            f_sum["valueAtRisk"] += float(p.awb.declared_value or 0) if p.awb else 0
            
            if p.awb and p.awb.shipment:
                for s in p.awb.shipment:
                    f_sum["shipmentIds"].add(s.s_no)
            if p.risk_reasons and isinstance(p.risk_reasons, list):
                f_sum["reasons"].extend(p.risk_reasons)
        
        for fid, summary in flight_map.items():
            summary["shipmentIds"] = list(summary["shipmentIds"])
            summary["reasons"] = list(set(summary["reasons"]))
            flight_summaries.append(summary)

    return {
        "items": paginated_items,
        "total_items": total_items,
        "total_by_type": {
            "DISRUPTION": total_disruptions,
            "PREDICTED_OFFLOAD": total_predictions,
            "PENDING_POOL": total_pool
        },
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "has_next_page": page < total_pages
        },
        "workflow_control": workflow_control,
        "flight_summaries": flight_summaries
    }


# --- 3. MAPPERS ---

def _map_offload_to_work_item(offload: models.Offload, item_type: str):
    manifest = offload.uld.awb_uld_flight[0] if offload.uld and offload.uld.awb_uld_flight else None
    awb = manifest.awb if manifest else None
    
    # Base fields
    result = schemas.WorkItem(
        work_item_id=str(offload.offload_id),
        type=item_type,
        status=offload.status or "PENDING",
        uld=schemas.ULDInfo(
            uld_id=offload.uld.uld_no,
            uld_type=offload.uld.loadtype_loadtype.l_type or "UNK" if offload.uld and offload.uld.loadtype_loadtype else "UNK",
            weight_kg=float(manifest.weight or 0) if manifest else 0.0,
            volume_m3=float(manifest.volume or 0) if manifest else 0.0,
            origin=offload.flight.origin or "UNK",
            destination=offload.flight.dest or "UNK"
        ),
        master_airway_bill=schemas.MAWBInfo(
            number=awb.awb_no if awb else "UNKNOWN",
            document_id=f"MAWB-DOC-{awb.awb_no}" if awb else "UNKNOWN",
            display_label=f"MAWB {awb.awb_no}" if awb else "UNKNOWN"
        ),
        flight=schemas.FlightInfo(
            flight_id=offload.flight.flight_id,
            origin=offload.flight.origin or "UNK",
            destination=offload.flight.dest or "UNK",
            scheduled_departure=offload.flight.std.isoformat() + "Z" if offload.flight.std else "",
            aircraft_type=offload.flight.aircraft_aircraft.aircraft_type if offload.flight.aircraft_aircraft else None
        ),
        cargo=schemas.CargoInfo(
            shipment_ids=[s.s_no for s in awb.shipment] if awb else [],
            weight_kg=float(manifest.weight or 0) if manifest else 0.0,
            volume_m3=float(manifest.volume or 0) if manifest else 0.0,
            handling_requirements=["GENERAL_CARGO"],
            commodity=None,
            value_at_risk=float(awb.declared_value or 0) if awb else 0.0,
            currency="HKD"
        ),
        document=schemas.DocumentInfo(
            document_id=f"MAWB-DOC-{awb.awb_no}" if awb else "UNKNOWN",
            version=awb.document_version or 1 if awb else 1,
            updated_at=datetime.now(timezone.utc).isoformat() + "Z"
        ),
        updated_at=datetime.now(timezone.utc).isoformat() + "Z"
    )

    # Type-specific data
    if item_type == "DISRUPTION":
        result.confirmed_offload = schemas.ConfirmedOffload(
            confirmed_at=offload.created_at.isoformat() + "Z" if offload.created_at else "",
            source="OPERATIONAL_EVENT",
            source_event_id=f"EVT-{offload.offload_id}",
            reason=offload.reason or "System detected disruption"
        )
        result.case_summary = schemas.CaseSummary(
            disruption=offload.reason or "Operational disruption",
            capacity_constraint="Weight limit exceeded",
            delivery_commitment=f"Customer delivery commitment: {awb.contract_ddl.strftime('%Y-%m-%d')}" if awb and awb.contract_ddl else "Unknown",
            baseline_expected_loss=float(awb.declared_value or 0) * 1.25 if awb else 0.0,
            currency="HKD"
        )
    elif item_type == "PENDING_POOL":
        result.pool = schemas.PoolInfo(
            source="DOMINO_OFFLOAD",
            source_flight_id=offload.flight_id,
            eligible_for_no_show_replacement=True,
            priority_hold=(offload.status == "PRIORITY_HOLD"), # True if held
            disposition=offload.status or "AVAILABLE" # Will output PRIORITY_HOLD or NO_SHOW_REVIEW
        )

    return result

def _map_prediction_to_work_item(pred: models.AwbUldFlight):
    safe_reasons = pred.risk_reasons if isinstance(pred.risk_reasons, list) else []
    
    result = schemas.WorkItem(
        work_item_id=f"{pred.awb_awb_no}-{pred.uld_uld_no}",
        type="PREDICTED_OFFLOAD",
        status="PREVENTIVE_ACTION_REQUIRED" if float(pred.offload_probability or 0) >= 0.90 else "AT_RISK",
        uld=schemas.ULDInfo(
            uld_id=pred.uld_uld_no,
            uld_type=pred.uld.loadtype_loadtype.l_type or "UNK" if pred.uld and pred.uld.loadtype_loadtype else "UNK",
            weight_kg=float(pred.weight or 0),
            volume_m3=float(pred.volume or 0),
            origin=pred.awb.origin or "UNK",
            destination=pred.awb.dest or "UNK"
        ),
        master_airway_bill=schemas.MAWBInfo(
            number=pred.awb_awb_no,
            document_id=f"MAWB-DOC-{pred.awb_awb_no}",
            display_label=f"MAWB {pred.awb_awb_no}"
        ),
        flight=schemas.FlightInfo(
            flight_id=pred.flight_flight.flight_id,
            origin=pred.flight_flight.origin or "UNK",
            destination=pred.flight_flight.dest or "UNK",
            scheduled_departure=pred.flight_flight.std.isoformat() + "Z" if pred.flight_flight.std else "",
            aircraft_type=pred.flight_flight.aircraft_aircraft.aircraft_type if pred.flight_flight.aircraft_aircraft else None
        ),
        cargo=schemas.CargoInfo(
            shipment_ids=[s.s_no for s in pred.awb.shipment],
            weight_kg=float(pred.weight or 0),
            volume_m3=float(pred.volume or 0),
            handling_requirements=["GENERAL_CARGO"],
            commodity=None,
            value_at_risk=float(pred.awb.declared_value or 0),
            currency="HKD"
        ),
        document=schemas.DocumentInfo(
            document_id=f"MAWB-DOC-{pred.awb_awb_no}",
            version=pred.awb.document_version or 1 if pred.awb else 1,
            updated_at=datetime.now(timezone.utc).isoformat() + "Z"
        ),
        updated_at=datetime.now(timezone.utc).isoformat() + "Z"
    )

    result.forecast = schemas.ForecastInfo(
        offload_probability=float(pred.offload_probability or 0),
        capacity_shortfall_kg=600.0, 
        confidence=int(float(pred.forecast_confidence or 0) * 100),
        forecasted_at=pred.forecast_timestamp.isoformat() + "Z" if pred.forecast_timestamp else datetime.now(timezone.utc).isoformat() + "Z",
        reasons=safe_reasons,
        forecast_cargo_capacity_kg=12000.0, 
        booked_weight_kg=12600.0, 
        value_at_risk=float(pred.awb.declared_value or 0),
        currency="HKD"
    )

    result.case_summary = schemas.CaseSummary(
        disruption="Ramp weather and updated fuel requirement", # Mocked or parse from safe_reasons
        capacity_constraint="Forecast cargo capacity is 600 kg below booked weight",
        delivery_commitment=f"Customer delivery commitment: {pred.awb.contract_ddl.strftime('%Y-%m-%d')}" if pred.awb.contract_ddl else "Unknown",
        baseline_expected_loss=float(pred.awb.declared_value or 0) * 1.25,
        currency="HKD"
    )

    return result

# --- ENDPOINT 2: GET SINGLE WORK ITEM ---
@router.get("/api/v1/recovery-solutions/{work_item_id}", response_model=schemas.RecoverySolutionDetailResponse)
def get_recovery_solution_detail(work_item_id: str, db: Session = Depends(get_db)):
    workflow_control = get_workflow_control(db)
    work_item = None
    
    # 1. Identify and fetch the Work Item
    # If the ID is purely digits, it's an Offload (Disruption or Pool)
    if work_item_id.isdigit():
        offload = db.query(models.Offload).options(
            joinedload(models.Offload.uld).joinedload(models.Uld.loadtype_loadtype),
            joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft)
        ).filter(models.Offload.offload_id == int(work_item_id)).first()
        
        if offload:
            if offload.source_type == 'Disrupted':
                work_item = _map_offload_to_work_item(offload, "DISRUPTION")
            elif offload.source_type == 'Displaced':
                work_item = _map_offload_to_work_item(offload, "PENDING_POOL")
    
    # If it's not an Offload, assume it's a Prediction (AwbUldFlight) formatted as "AWB-ULD"
    else:
        if '-' in work_item_id:
            awb_no, uld_no = work_item_id.rsplit('-', 1)
            
            pred = db.query(models.AwbUldFlight).options(
                joinedload(models.AwbUldFlight.awb).joinedload(models.Awb.shipment),
                joinedload(models.AwbUldFlight.uld).joinedload(models.Uld.loadtype_loadtype),
                joinedload(models.AwbUldFlight.flight_flight).joinedload(models.Flight.aircraft_aircraft)
            ).filter(
                models.AwbUldFlight.awb_awb_no == awb_no,
                models.AwbUldFlight.uld_uld_no == uld_no
            ).first()
            
            if pred and pred.offload_probability and float(pred.offload_probability) >= 0.80:
                work_item = _map_prediction_to_work_item(pred)
    
    if not work_item:
        raise HTTPException(status_code=404, detail="Work item not found")

    # 2. Fetch Review History
    decisions = db.query(models.RecoveryDecision).filter(
        models.RecoveryDecision.work_item_id == work_item_id
    ).order_by(models.RecoveryDecision.reviewed_at.desc()).all()
    
    history = []
    for d in decisions:
        history.append(schemas.ReviewHistoryEntry(
            decision_id=d.decision_id,
            type=d.work_item_type,
            uld_id=d.uld_id,
            master_airway_bill_number=d.awb_no,
            document_version=d.document_version,
            option_id=d.option_id,
            # NEW FIELDS ADDED:
            target_work_item_id=d.target_work_item_id,
            confirmed_no_show_event_id=d.confirmed_no_show_event_id,
            # ------------------
            decision=d.decision,
            reason=d.reason or "No reason provided", 
            reviewer_id=d.reviewer_id,
            reviewed_at=d.reviewed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if d.reviewed_at else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ))


    # 3. Construct the detailed response
    response_dict = work_item.model_dump()
    response_dict["workflow_control"] = workflow_control
    response_dict["review_history"] = history
    
    return response_dict

# --- HELPER: Generate Mock Options (Used by Endpoint 3 & 4) ---
def _generate_mock_options(uld_no: str):
    now_utc = datetime.now(timezone.utc)
    eta1 = (now_utc + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    eta2 = (now_utc + timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M:%SZ")
    eta3 = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    option_1 = schemas.RoutingOption(
        option_id=f"OPT-{uld_no}-01",
        option_type="reroute",
        recovery_legs=[
            schemas.RecoveryLegInfo(**{"from": "HKG", "to": "DOH", "operating_flight_id": "CX660"}),
            schemas.RecoveryLegInfo(**{"from": "DOH", "to": "FRA", "operating_flight_id": "QR001"})
        ],
        feasibility_status="Feasible",
        feasibility_percentage=97,
        usable_capacity_kg=1200.0,
        capacity_status="Confirmed",
        fuel_constraint="None",
        weather_exposure="Low",
        expected_arrival=eta1,
        expected_delay_minutes=180,
        recovery_handling_cost=4760.0,
        total_modeled_expected_loss=50780.0,
        cost_breakdown=schemas.CostBreakdown(
            cargo_customer_impact=40000.0,
            cathay_contribution_impact=6020.0,
            recovery_and_handling_cost=4760.0,
            delay_and_service_impact=0.0,
            downstream_network_impact=0.0,
            total_modeled_expected_loss=50780.0,
            currency="HKD"
        ),
        missing_data=[],
        network_impact="Confirmed capacity; no displaced cargo identified."
    )

    option_2 = schemas.RoutingOption(
        option_id=f"OPT-{uld_no}-02",
        option_type="wait_for_next_flight",
        recovery_legs=[
            schemas.RecoveryLegInfo(**{"from": "HKG", "to": "FRA", "operating_flight_id": "CX291"})
        ],
        feasibility_status="Feasible",
        feasibility_percentage=100,
        usable_capacity_kg=1400.0,
        capacity_status="Confirmed",
        fuel_constraint="None",
        weather_exposure="Clear",
        expected_arrival=eta2,
        expected_delay_minutes=600,
        recovery_handling_cost=1720.0,
        total_modeled_expected_loss=53980.0,
        cost_breakdown=schemas.CostBreakdown(
            cargo_customer_impact=52260.0,
            cathay_contribution_impact=0.0,
            recovery_and_handling_cost=1720.0,
            delay_and_service_impact=0.0,
            downstream_network_impact=0.0,
            total_modeled_expected_loss=53980.0,
            currency="HKD"
        ),
        missing_data=[],
        network_impact="No downstream displacement identified."
    )

    option_3 = schemas.RoutingOption(
        option_id=f"OPT-{uld_no}-03",
        option_type="rebook",
        recovery_legs=[
            schemas.RecoveryLegInfo(**{"from": "HKG", "to": "FRA", "operating_flight_id": "CX289"})
        ],
        feasibility_status="Infeasible",
        feasibility_percentage=10,
        usable_capacity_kg=0.0,
        capacity_status="Capacity Confirmation Pending",
        fuel_constraint="Weather penalty expected",
        weather_exposure="High",
        expected_arrival=eta3,
        expected_delay_minutes=1440,
        recovery_handling_cost=5000.0,
        total_modeled_expected_loss=85000.0,
        cost_breakdown=schemas.CostBreakdown(
            cargo_customer_impact=80000.0,
            cathay_contribution_impact=0.0,
            recovery_and_handling_cost=5000.0,
            delay_and_service_impact=0.0,
            downstream_network_impact=0.0,
            total_modeled_expected_loss=85000.0,
            currency="HKD"
        ),
        missing_data=["Station acceptance unconfirmed", "Payload limit exceeded"],
        network_impact="None"
    )

    return [option_1, option_2, option_3]

# --- ENDPOINT 3: GET CANDIDATE ROUTING OPTIONS ---
@router.get("/api/v1/recovery-solutions/{work_item_id}/options", response_model=schemas.OptionsResponse)
def get_recovery_options(work_item_id: str, db: Session = Depends(get_db)):
    
    item_type = None
    uld_no = None
    awb_no = None
    
    # 1. Validate Work Item & Determine Type
    if work_item_id.isdigit():
        offload = db.query(models.Offload).filter(models.Offload.offload_id == int(work_item_id)).first()
        if not offload:
            raise HTTPException(status_code=404, detail="Work item not found")
            
        if offload.source_type == 'Displaced':
            raise HTTPException(status_code=400, detail="Pool ULDs are evaluated against a target using the replacement-candidates endpoint. They do not get routing options.")
        elif offload.source_type == 'Disrupted':
            item_type = "DISRUPTION"
            uld_no = offload.uld_no
            # Find AWB for this ULD
            manifest = db.query(models.AwbUldFlight).filter(models.AwbUldFlight.uld_uld_no == uld_no).first()
            awb_no = manifest.awb_awb_no if manifest else "UNKNOWN"
    else:
        if '-' in work_item_id:
            awb_no, uld_no = work_item_id.rsplit('-', 1)
            pred = db.query(models.AwbUldFlight).filter(
                models.AwbUldFlight.awb_awb_no == awb_no,
                models.AwbUldFlight.uld_uld_no == uld_no
            ).first()
            if not pred or not pred.offload_probability or float(pred.offload_probability) < 0.80:
                raise HTTPException(status_code=404, detail="Predicted offload work item not found or below threshold")
            item_type = "PREDICTED_OFFLOAD"

    if not item_type:
        raise HTTPException(status_code=400, detail="Invalid work item format")

    all_options = _generate_mock_options(uld_no or "UNKNOWN")
    # 3. Calculate Best Options
    # Contract: Feasible option IDs with the lowest totalModeledExpectedLoss
    feasible_options = [o for o in all_options if o.feasibility_status == "Feasible"]
    
    best_options = []
    if feasible_options:
        min_loss = min(o.total_modeled_expected_loss for o in feasible_options)
        best_options = [o.option_id for o in feasible_options if o.total_modeled_expected_loss == min_loss]

    # 4. Return Response
    return {
        "work_item_id": work_item_id,
        "type": item_type,
        "best_options": best_options,
        "options": all_options,
        "currency": "HKD"
    }

@router.get("/api/v1/recovery-solutions/{work_item_id}/options/{option_id}", response_model=schemas.OptionDetailResponse)
def get_option_detail(work_item_id: str, option_id: str, db: Session = Depends(get_db)):
    
    # 1. Validate Work Item & Extract References
    uld_no = "UNKNOWN"
    awb_no = "UNKNOWN"
    awb_obj = None
    
    if work_item_id.isdigit():
        offload = db.query(models.Offload).filter(models.Offload.offload_id == int(work_item_id)).first()
        if not offload:
            raise HTTPException(status_code=404, detail="Work item not found")
        if offload.source_type == 'Displaced':
            raise HTTPException(status_code=400, detail="Pool ULDs do not have routing options.")
        
        uld_no = offload.uld_no
        manifest = db.query(models.AwbUldFlight).filter(models.AwbUldFlight.uld_uld_no == uld_no).first()
        if manifest:
            awb_no = manifest.awb_awb_no
            awb_obj = manifest.awb
    else:
        if '-' in work_item_id:
            awb_no, uld_no = work_item_id.rsplit('-', 1)
            pred = db.query(models.AwbUldFlight).filter(
                models.AwbUldFlight.awb_awb_no == awb_no,
                models.AwbUldFlight.uld_uld_no == uld_no
            ).first()
            if not pred:
                raise HTTPException(status_code=404, detail="Prediction not found")
            awb_obj = pred.awb

    # 2. Fetch the specific option from our mock generator
    all_options = _generate_mock_options(uld_no)
    selected_option = next((o for o in all_options if o.option_id == option_id), None)
    
    if not selected_option:
        raise HTTPException(status_code=404, detail="Option not found for this work item")

    # 3. Construct References (ULD & MAWB)
    uld_info = schemas.ULDInfo(
        uld_id=uld_no,
        uld_type=uld_no[:3] if len(uld_no) >= 3 else "UNK", # Extract AKE/PMC
        weight_kg=selected_option.usable_capacity_kg, # Mocking weight for reference
        volume_m3=5.8,
        origin="HKG", # Hardcoded mock for now
        destination="FRA"
    )

    mawb_info = schemas.MAWBInfo(
        number=awb_no,
        document_id=f"MAWB-DOC-{awb_no}",
        display_label=f"MAWB {awb_no}"
    )

    # 4. Construct Network Domino Effect
    domino_effect = schemas.NetworkDominoEffect(
        affected_flights=["CX291"] if selected_option.option_type == "reroute" else [],
        affected_uld_ids=["PMC99999CX"] if selected_option.option_type == "reroute" else [],
        affected_shipment_ids=["SHP-1234"] if selected_option.option_type == "reroute" else [],
        additional_capacity_shortfall_kg=500.0 if selected_option.option_type == "reroute" else 0.0,
        summary="Displaced cargo from CX291 requires re-routing to next available flight." if selected_option.option_type == "reroute" else "No downstream impact."
    )

    # 5. Construct Scenario Sensitivity (Showing what happens if weather gets worse)
    scenario_1 = schemas.ScenarioSensitivity(
        input_change="Headwind increases by 20 knots",
        feasibility_status="Infeasible",
        cost_breakdown=schemas.CostBreakdown(
            cargo_customer_impact=65000.0,
            cathay_contribution_impact=0.0,
            recovery_and_handling_cost=4760.0,
            delay_and_service_impact=0.0,
            downstream_network_impact=0.0,
            total_modeled_expected_loss=69760.0,
            currency="HKD"
        )
    )

    # 6. Construct Explanation
    explanation = schemas.ExplanationInfo(
        summary=f"Route via {selected_option.option_type} is currently optimal.",
        reasons=["Lowest modeled expected loss", "Confirmed capacity at transit hub"],
        assumptions=["Transit customs clearance takes < 2 hours", "Trucking connection at DOH is available"],
        missing_data=selected_option.missing_data
    )

    # 7. Return combined response
    return schemas.OptionDetailResponse(
        work_item_id=work_item_id,
        option=selected_option,
        uld=uld_info,
        master_airway_bill=mawb_info,
        cost_breakdown=selected_option.cost_breakdown,
        network_domino_effect=domino_effect,
        scenario_sensitivity=[scenario_1],
        explanation=explanation
    )
# --- ENDPOINT 5: CONFIRM OFFLOAD ---
@router.post("/api/v1/recovery-solutions/{work_item_id}/offload-confirmations", response_model=schemas.WorkItem)
def confirm_offload(
    work_item_id: str, 
    payload: schemas.OffloadConfirmationRequest, 
    db: Session = Depends(get_db)
):
    # 1. Validation: Source and Reviewer
    if payload.source not in ["OPERATIONAL_EVENT", "AUTHORIZED_DECISION"]:
        raise HTTPException(status_code=422, detail="Invalid source. Must be OPERATIONAL_EVENT or AUTHORIZED_DECISION.")
    
    if payload.source == "AUTHORIZED_DECISION" and not payload.reviewer_id:
        raise HTTPException(status_code=422, detail="reviewerId is required for AUTHORIZED_DECISION source.")

    # 2. Idempotency Check
    # If this event ID was already processed, return the existing Offload record
    existing_event_offload = db.query(models.Offload).filter(
        models.Offload.source_event_id == payload.source_event_id
    ).first()
    
    if existing_event_offload:
        return _map_offload_to_work_item(existing_event_offload, "DISRUPTION")

    # 3. Validation: Work Item Type
    # This endpoint only transitions PREDICTED_OFFLOAD. 
    # If the ID is purely digits, it's already an Offload (Disruption/Pool).
    if work_item_id.isdigit():
        raise HTTPException(status_code=422, detail="Invalid transition. Work item is already an Offload record.")

    if '-' not in work_item_id:
        raise HTTPException(status_code=404, detail="Work item not found or invalid format.")

    awb_no, uld_no = work_item_id.rsplit('-', 1)
    
    # 4. Fetch the Prediction
    pred = db.query(models.AwbUldFlight).options(
        joinedload(models.AwbUldFlight.awb)
    ).filter(
        models.AwbUldFlight.awb_awb_no == awb_no,
        models.AwbUldFlight.uld_uld_no == uld_no
    ).first()

    if not pred:
        raise HTTPException(status_code=404, detail="Predicted offload work item not found.")
        
    # Check if it's already transitioned
    if pred.status in ['CLEARED', 'TRANSITIONED']:
        raise HTTPException(status_code=422, detail="Work item has already been cleared or transitioned.")

    # 5. Optimistic Concurrency Check
    awb = pred.awb
    current_version = awb.document_version or 1
    if current_version != payload.document_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, 
            detail={"message": "Stale document version. Please refresh.", "currentVersion": current_version}
        )

    # 6. Atomic Transition
    try:
        # A. Update the AWB version
        awb.document_version = current_version + 1

        # B. Mark the prediction as transitioned so it leaves the prediction queue
        pred.status = 'TRANSITIONED'

        # C. Create (or update) the Offload record
        # Check if an offload record already exists (e.g., created by the scanner for this flight/uld)
        offload = db.query(models.Offload).filter(
            models.Offload.uld_no == uld_no,
            models.Offload.flight_id == pred.flight_flight_id
        ).first()

        if offload:
            # Update existing
            offload.source_type = 'Disrupted'
            offload.status = 'RECOVERY_REQUIRED'
            offload.reason = payload.reason
            offload.source_event_id = payload.source_event_id
        else:
            # Create new
            offload = models.Offload(
                uld_no=uld_no,
                flight_id=pred.flight_flight_id,
                source_type='Disrupted',
                status='RECOVERY_REQUIRED',
                reason=payload.reason,
                source_event_id=payload.source_event_id
            )
            db.add(offload)

        db.commit()
        db.refresh(offload)

        # 7. Return the updated work item (now a DISRUPTION)
        return _map_offload_to_work_item(offload, "DISRUPTION")

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database transition failed: {str(e)}")




# --- ENDPOINT 6: EVALUATE PENDING POOL CANDIDATES ---
@router.get("/api/v1/recovery-solutions/{work_item_id}/replacement-candidates", response_model=schemas.ReplacementCandidateResponse)
def get_replacement_candidates(
    work_item_id: str, 
    target_work_item_id: str = Query(..., description="The active DISRUPTION or PREDICTED_OFFLOAD to be replaced"),
    purpose: str = Query(..., pattern="^(PRIORITY_ALTERNATIVE|NO_SHOW_REPLACEMENT)$"),
    confirmed_no_show_event_id: Optional[str] = Query(None, description="Required if purpose is NO_SHOW_REPLACEMENT"),
    db: Session = Depends(get_db)
):
    # =========================================================================
    # 1. VALIDATE CANDIDATE (The Pool ULD)
    # =========================================================================
    # A pool ULD must be an Offload record with source_type 'Displaced'
    if not work_item_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid candidate format. Must be a numeric Offload ID.")
        
    candidate_offload = db.query(models.Offload).options(
        joinedload(models.Offload.uld),
        joinedload(models.Offload.flight)
    ).filter(
        models.Offload.offload_id == int(work_item_id),
        models.Offload.source_type == 'Displaced'
    ).first()

    if not candidate_offload:
        raise HTTPException(status_code=404, detail="Pending pool candidate not found.")
    
    candidate_uld = candidate_offload.uld

    # =========================================================================
    # 2. VALIDATE TARGET (The Disruption or Prediction)
    # =========================================================================
    target_flight = None
    target_uld_no = None
    
    if target_work_item_id.isdigit():
        # It's a Disruption (Offload)
        target_offload = db.query(models.Offload).options(
            joinedload(models.Offload.flight)
        ).filter(
            models.Offload.offload_id == int(target_work_item_id),
            models.Offload.source_type == 'Disrupted'
        ).first()
        
        if target_offload:
            target_flight = target_offload.flight
            target_uld_no = target_offload.uld_no
            
    else:
        # It's a Prediction (AwbUldFlight)
        if '-' in target_work_item_id:
            awb_no, uld_no = target_work_item_id.rsplit('-', 1)
            pred = db.query(models.AwbUldFlight).options(
                joinedload(models.AwbUldFlight.flight_flight)
            ).filter(
                models.AwbUldFlight.awb_awb_no == awb_no,
                models.AwbUldFlight.uld_uld_no == uld_no,
                models.AwbUldFlight.offload_probability >= 0.80
            ).first()
            
            if pred:
                target_flight = pred.flight_flight
                target_uld_no = pred.uld_uld_no

    if not target_flight or not target_uld_no:
        raise HTTPException(status_code=404, detail="Active target (Disruption or Prediction) not found.")

    # =========================================================================
    # 3. VALIDATE NO-SHOW EVENT (If applicable)
    # =========================================================================
    if purpose == "NO_SHOW_REPLACEMENT":
        if not confirmed_no_show_event_id:
            raise HTTPException(status_code=422, detail="confirmedNoShowEventId is required for NO_SHOW_REPLACEMENT purpose.")
        
        # Mock validation: In a real system, you'd query an Event table here.
        # We assume if the event ID starts with "NS-", it's valid.
        if not confirmed_no_show_event_id.startswith("NS-"):
            raise HTTPException(status_code=422, detail="Invalid or unvalidated no-show event ID.")

    # =========================================================================
    # 4. EVALUATE COMPATIBILITY
    # =========================================================================
    reasons = []
    missing_data = []
    eligibility = "ELIGIBLE"
    
    # A. Route/Destination Compatibility
    # The pool ULD's previous destination should match the target flight's destination
    route_compatible = True
    if candidate_offload.flight and target_flight:
        if candidate_offload.flight.dest != target_flight.dest:
            route_compatible = False
            eligibility = "INELIGIBLE"
            reasons.append(f"Route mismatch: Pool ULD was destined for {candidate_offload.flight.dest}, target needs {target_flight.dest}.")
    else:
        route_compatible = False
        missing_data.append("Missing flight origin/destination data.")

    # B. Handling Compatibility
    # Check if the ULD type matches (e.g., AKE for lower deck, PMC for main deck)
    handling_compatible = True
    if candidate_uld and candidate_uld.loadtype_loadtype_id:
        # Get target ULD type
        target_uld = db.query(models.Uld).filter(models.Uld.uld_no == target_uld_no).first()
        if target_uld and target_uld.loadtype_loadtype_id != candidate_uld.loadtype_loadtype_id:
            handling_compatible = False
            eligibility = "INELIGIBLE"
            reasons.append("ULD type mismatch (e.g., Main Deck vs Lower Deck).")
    else:
        handling_compatible = False
        missing_data.append("Missing ULD loadtype data.")

    # C. Weight and Volume Capacity
    # We mock the available capacity here. In production, you'd query the target flight's remaining payload.
    # We set it to 0.0 and add to missing_data if we can't calculate it.
    weight_capacity_available_kg = 1500.0 # Mocked limit
    volume_capacity_available_m3 = 10.0  # Mocked limit
    
    if weight_capacity_available_kg <= 0:
        eligibility = "INELIGIBLE"
        reasons.append("Target flight has no remaining weight capacity.")

    # Final eligibility state adjustment
    if eligibility != "INELIGIBLE" and missing_data:
        eligibility = "UNKNOWN"

    # =========================================================================
    # 5. RETURN RESPONSE
    # =========================================================================
    return {
        "candidate_work_item_id": str(candidate_offload.offload_id),
        "target_work_item_id": target_work_item_id,
        "eligibility": eligibility,
        "reasons": reasons,
        "missing_data": missing_data,
        "route_compatible": route_compatible,
        "handling_compatible": handling_compatible,
        "weight_capacity_available_kg": weight_capacity_available_kg,
        "volume_capacity_available_m3": volume_capacity_available_m3,
        "requires_confirmed_no_show": (purpose == "NO_SHOW_REPLACEMENT")
    }

# --- ENDPOINT 7: RECORD DECISION ---
@router.post("/api/v1/recovery-solutions/{work_item_id}/decisions", response_model=schemas.DecisionResponse, status_code=status.HTTP_201_CREATED)
def record_decision(
    work_item_id: str, 
    payload: schemas.DecisionRequest, 
    response: Response,
    db: Session = Depends(get_db)
):
    wf = get_workflow_control(db)
    
    # =========================================================================
    # 1. GATING VALIDATION
    # =========================================================================
    if payload.type == "PREDICTED_OFFLOAD" and wf["active_priority"] == 1:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED, 
            detail={"code": "HIGHER_PRIORITY_WORK_OPEN", "blockingWorkItemIds": wf["blocking_work_item_ids"]}
        )
    
    if payload.type == "PENDING_POOL" and wf["active_priority"] in [1, 2]:
        # HOLD_FOR_PRIORITY is allowed to protect inventory, but others are blocked
        if payload.decision != "HOLD_FOR_PRIORITY":
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED, 
                detail={"code": "HIGHER_PRIORITY_WORK_OPEN", "blockingWorkItemIds": wf["blocking_work_item_ids"]}
            )

    # =========================================================================
    # 2. ITEM & ULD VALIDATION
    # =========================================================================
    item_uld_no = None
    item_type_db = None
    offload_record = None
    pred_record = None

    if work_item_id.isdigit():
        offload_record = db.query(models.Offload).filter(models.Offload.offload_id == int(work_item_id)).first()
        if not offload_record:
            raise HTTPException(status_code=404, detail="Work item not found")
        item_uld_no = offload_record.uld_no
        item_type_db = "DISRUPTION" if offload_record.source_type == 'Disrupted' else "PENDING_POOL"
    else:
        if '-' in work_item_id:
            awb_no, uld_no = work_item_id.rsplit('-', 1)
            pred_record = db.query(models.AwbUldFlight).filter(
                models.AwbUldFlight.awb_awb_no == awb_no,
                models.AwbUldFlight.uld_uld_no == uld_no
            ).first()
            if pred_record:
                item_uld_no = pred_record.uld_uld_no
                item_type_db = "PREDICTED_OFFLOAD"

    if not item_uld_no or not item_type_db:
        raise HTTPException(status_code=404, detail="Work item not found")

    if item_type_db != payload.type:
        raise HTTPException(status_code=422, detail=f"Type mismatch. Path item is {item_type_db}, payload is {payload.type}")

    if item_uld_no != payload.uld_id:
        raise HTTPException(status_code=422, detail="ULD ID in payload does not match the path item")

    # =========================================================================
    # 3. DECISION VALIDATION
    # =========================================================================
    allowed_decisions = {
        "DISRUPTION": ["APPROVE_FOR_REVIEW", "REJECT_OVERRIDE"],
        "PREDICTED_OFFLOAD": ["APPROVE_FOR_REVIEW", "REJECT_OVERRIDE"],
        "PENDING_POOL": ["HOLD_FOR_PRIORITY", "RELEASE_HOLD", "CONSIDER_FOR_NO_SHOW", "CLEAR_NO_SHOW_REVIEW"]
    }

    if payload.decision not in allowed_decisions[payload.type]:
        raise HTTPException(status_code=422, detail=f"Decision '{payload.decision}' is invalid for type '{payload.type}'")

    if payload.decision == "APPROVE_FOR_REVIEW" and not payload.option_id:
        raise HTTPException(status_code=422, detail="optionId is required for APPROVE_FOR_REVIEW")

    if payload.decision == "CONSIDER_FOR_NO_SHOW":
        if not payload.target_work_item_id or not payload.confirmed_no_show_event_id:
            raise HTTPException(status_code=422, detail="targetWorkItemId and confirmedNoShowEventId are required for CONSIDER_FOR_NO_SHOW")

    # =========================================================================
    # 4. OPTIMISTIC CONCURRENCY
    # =========================================================================
    awb = db.query(models.Awb).filter(models.Awb.awb_no == payload.master_airway_bill_number).first()
    if not awb:
        raise HTTPException(status_code=404, detail="MAWB not found")
        
    current_version = awb.document_version or 1
    if current_version != payload.document_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, 
            detail={"message": "Stale document version. Please refresh.", "currentVersion": current_version}
        )

    # =========================================================================
    # 5. DATABASE TRANSACTION
    # =========================================================================
    try:
        # A. Create Decision Record
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
            reviewed_at=datetime.fromisoformat(payload.reviewed_at.replace("Z", "+00:00")),
            target_work_item_id=payload.target_work_item_id,
            confirmed_no_show_event_id=payload.confirmed_no_show_event_id
        )
        db.add(new_decision)

        # B. Update Underlying Status (Pool Dispositions)
        if payload.type == "PENDING_POOL" and offload_record:
            if payload.decision == "HOLD_FOR_PRIORITY":
                offload_record.status = "PRIORITY_HOLD"
            elif payload.decision == "RELEASE_HOLD":
                offload_record.status = "AVAILABLE"
            elif payload.decision == "CONSIDER_FOR_NO_SHOW":
                offload_record.status = "NO_SHOW_REVIEW"
            elif payload.decision == "CLEAR_NO_SHOW_REVIEW":
                offload_record.status = "AVAILABLE"

        # C. Update Document Version
        awb.document_version = current_version + 1

        db.commit()
        db.refresh(new_decision)

        # D. Set Location Header
        response.headers["Location"] = f"/api/v1/recovery-solutions/{work_item_id}/decisions/{new_decision.decision_id}"

        # E. Return Response
        return schemas.DecisionResponse(
            decision_id=new_decision.decision_id,
            work_item_id=work_item_id,
            type=new_decision.work_item_type,
            uld_id=new_decision.uld_id,
            master_airway_bill_number=new_decision.awb_no,
            document_version=new_decision.document_version,
            decision=new_decision.decision,
            target_work_item_id=new_decision.target_work_item_id,
            reviewed_at=new_decision.reviewed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if new_decision.reviewed_at else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database transaction failed: {str(e)}")



app.include_router(router)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "API is running"}