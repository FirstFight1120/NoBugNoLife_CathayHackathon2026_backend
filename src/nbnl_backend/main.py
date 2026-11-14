from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_, or_
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional
from fastapi import FastAPI, APIRouter, Query, Depends, HTTPException, status, Response
from fastapi.middleware.cors import CORSMiddleware
import math



from src.nbnl_backend.db import get_db
from src.nbnl_backend import models, schemas
from src.nbnl_backend.bg_tasks_pri_risk import run_batch_update_job
from src.nbnl_backend.bg_tasks_scanner import run_disruption_scanner


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    
    # 1. Priority & Risk ML Calculator (Runs every 60 minutes)
    #scheduler.add_job(
    #    run_batch_update_job, 
    #    'interval', 
    #    minutes=60, 
    #    next_run_time=datetime.now(timezone.utc) # Run immediately on startup
    #)
    
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins (good for dev/hackathons)
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, OPTIONS, etc.)
    allow_headers=["*"],  # Allows all headers 
)

router = APIRouter()
TERMINAL_STATUSES = ['RECOVERED', 'CLOSED', 'RETIRED', 'CLEARED']

def get_workflow_control(db: Session) -> dict:
    active_disruptions = db.query(models.Offload).filter(
        models.Offload.source_type == 'Disrupted', 
        models.Offload.status.notin_(['RECOVERED', 'CLOSED'])
    ).count()
    
    active_predictions = db.query(models.Offload).filter(
        models.Offload.source_type == 'Predicted',
        models.Offload.status != 'CLEARED'
    ).count()

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
        ).all()
        blocking_ids = [str(b.offload_id) for b in blocking]
    elif active_predictions > 0:
        active_priority = 2
        blocking = db.query(models.Offload).filter(
            models.Offload.source_type == 'Predicted',
            models.Offload.status != 'CLEARED'
        ).limit(3).all()
        blocking_ids = [str(b.offload_id) for b in blocking]
    elif active_pool > 0:
        active_priority = 3
        blocking = db.query(models.Offload).filter(
            models.Offload.source_type == 'Displaced',
            models.Offload.status != 'RETIRED'
        ).limit(3).all()
        blocking_ids = [str(b.offload_id) for b in blocking]

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
    master_airway_bill_number: Optional[str] = Query(None, alias="masterAirwayBillNumber"),
    flight_id: Optional[str] = Query(None, alias="flightId"),
    status: Optional[str] = None,
    include_closed: bool = Query(False, alias="includeClosed"),
    db: Session = Depends(get_db)
):
    # 1. Get Gating Logic
    workflow_control_data = get_workflow_control(db)
    workflow_control = schemas.WorkflowControl(**workflow_control_data)

    # 2. Build the Base Query (applies to all filters EXCEPT `type`)
    base_query = db.query(models.Offload)
    
    # Filter by AWB (Requires joining the ternary table)
    if master_airway_bill_number:
        base_query = base_query.join(
            models.AwbUldFlight,
            and_(
                models.Offload.uld_no == models.AwbUldFlight.uld_uld_no,
                models.Offload.flight_id == models.AwbUldFlight.flight_flight_id
            )
        ).filter(models.AwbUldFlight.awb_awb_no == master_airway_bill_number)

    if flight_id:
        base_query = base_query.filter(models.Offload.flight_id == flight_id)
        
    if status:
        base_query = base_query.filter(models.Offload.status == status)
        
    if not include_closed:
        base_query = base_query.filter(models.Offload.status.notin_(TERMINAL_STATUSES))

    # 3. Calculate Counts for ALL types (independent of the `type` filter)
    total_by_type = {
        "DISRUPTION": base_query.filter(models.Offload.source_type == 'Disrupted').count(),
        "PREDICTED_OFFLOAD": base_query.filter(models.Offload.source_type == 'Predicted').count(),
        "PENDING_POOL": base_query.filter(models.Offload.source_type == 'Displaced').count()
    }

    # 4. Apply the specific `type` filter (if provided) for fetching items
    query = base_query
    if type == "DISRUPTION":
        query = query.filter(models.Offload.source_type == 'Disrupted')
    elif type == "PREDICTED_OFFLOAD":
        query = query.filter(models.Offload.source_type == 'Predicted')
    elif type == "PENDING_POOL":
        query = query.filter(models.Offload.source_type == 'Displaced')

    # 5. Execute Query with Pagination
    total_items = query.count()
    total_pages = math.ceil(total_items / limit) if total_items > 0 else 1
    
    offloads = query.options(
        joinedload(models.Offload.uld).joinedload(models.Uld.loadtype_loadtype),
        joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft)
    ).order_by(models.Offload.created_at.desc()).offset((page - 1) * limit).limit(limit).all()

    # 6. Map to WorkItems
    items = []
    for o in offloads:
        items.append(_map_offload_to_work_item(db, o))

    # 7. Generate Flight Summaries (Only for PREDICTED_OFFLOAD)
    flight_summaries = []
    if type in [None, "PREDICTED_OFFLOAD"]:
        # We query all predictions (ignoring pagination) for this specific view
        pred_query = base_query.filter(models.Offload.source_type == 'Predicted')
        
        # Group by flight_id
        flight_groups = pred_query.options(
            joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft),
            joinedload(models.Offload.uld)
        ).all()
        
        grouped_data = {}
        for p in flight_groups:
            fid = p.flight_id
            if fid not in grouped_data:
                grouped_data[fid] = {
                    "flightId": fid,
                    "origin": p.flight.origin or "UNK",
                    "destination": p.flight.dest or "UNK",
                    "scheduledDeparture": p.flight.std.isoformat() + "Z" if p.flight.std else "",
                    "aircraftType": p.flight.aircraft_aircraft.aircraft_type if p.flight.aircraft_aircraft else None,
                    "offloadProbability": 0.0,
                    "capacityShortfallKg": 600.0, # Mocked for now
                    "forecastCargoCapacityKg": 12000.0, # Mocked
                    "bookedWeightKg": 12600.0, # Mocked
                    "affectedUldCount": 0,
                    "shipmentIds": set(),
                    "valueAtRisk": 0.0,
                    "currency": "HKD",
                    "confidence": 0,
                    "forecastedAt": "",
                    "reasons": set()
                }
            
            # Find the specific manifest record to get ML data
            manifest = db.query(models.AwbUldFlight).filter(
                models.AwbUldFlight.uld_uld_no == p.uld_no,
                models.AwbUldFlight.flight_flight_id == fid
            ).first()
            
            f_sum = grouped_data[fid]
            f_sum["affectedUldCount"] += 1
            
            if manifest:
                # Aggregate ML data
                prob = float(manifest.offload_probability or 0)
                f_sum["offloadProbability"] = max(f_sum["offloadProbability"], prob)
                f_sum["confidence"] = max(f_sum["confidence"], int(float(manifest.forecast_confidence or 0) * 100))
                
                ts = manifest.forecast_timestamp.isoformat() + "Z" if manifest.forecast_timestamp else ""
                if ts > f_sum["forecastedAt"]:
                    f_sum["forecastedAt"] = ts
                    
                if manifest.risk_reasons and isinstance(manifest.risk_reasons, list):
                    for r in manifest.risk_reasons:
                        f_sum["reasons"].add(str(r))
                
                # Aggregate AWB data
                if manifest.awb:
                    f_sum["valueAtRisk"] += float(manifest.awb.declared_value or 0)
                    for s in manifest.awb.shipment:
                        f_sum["shipmentIds"].add(s.s_no)

        # Clean up sets for JSON serialization
        for fid, summary in grouped_data.items():
            summary["shipmentIds"] = list(summary["shipmentIds"])
            summary["reasons"] = list(summary["reasons"])
            flight_summaries.append(summary)

    # 8. Return the Final Envelope
    return schemas.RecoverySolutionListResponse(
        items=items,
        total_items=total_items,
        total_by_type=total_by_type,
        pagination=schemas.Pagination(
            page=page,
            total_pages=total_pages,
            has_next_page=page < total_pages
        ),
        workflow_control=workflow_control,
        flight_summaries=flight_summaries
    )

# --- 3. MAPPERS ---

def _map_offload_to_work_item(db: Session, offload: models.Offload) -> schemas.WorkItem:
    # 1. Determine Type
    if offload.source_type == 'Disrupted':
        item_type = "DISRUPTION"
    elif offload.source_type == 'Predicted':
        item_type = "PREDICTED_OFFLOAD"
    else:
        item_type = "PENDING_POOL"

    # 2. Fetch Base Data
    manifest = db.query(models.AwbUldFlight).filter(
        models.AwbUldFlight.uld_uld_no == offload.uld_no,
        models.AwbUldFlight.flight_flight_id == offload.flight_id
    ).first()
    
    awb = manifest.awb if manifest else None

    result = schemas.WorkItem(
        work_item_id=str(offload.offload_id), # Always the numeric ID
        type=item_type,
        status=offload.status or "PENDING",
        uld=schemas.ULDInfo(
            uld_id=offload.uld.uld_no,
            uld_type=(offload.uld.loadtype_loadtype.l_type or "UNK") if offload.uld and offload.uld.loadtype_loadtype else "UNK",
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
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # 3. Type-Specific Mapping
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
        
    elif item_type == "PREDICTED_OFFLOAD":
        safe_reasons = manifest.risk_reasons if manifest and isinstance(manifest.risk_reasons, list) else ["Operational constraints"]
        
        # Fallback confidence to 85% if null
        confidence_val = int(float(manifest.forecast_confidence or 0.85) * 100) if manifest else 85
        
        result.forecast = schemas.ForecastInfo(
            offload_probability=float(manifest.offload_probability or 0) if manifest else 0.0,
            capacity_shortfall_kg=600.0, 
            confidence=confidence_val, # Used fallback here
            forecasted_at=manifest.forecast_timestamp.isoformat() + "Z" if manifest and manifest.forecast_timestamp else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=safe_reasons, # Used fallback here
            forecast_cargo_capacity_kg=12000.0, 
            booked_weight_kg=12600.0, 
            value_at_risk=float(awb.declared_value or 0) if awb else 0.0,
            currency="HKD"
        )

    elif item_type == "PENDING_POOL":
        result.pool = schemas.PoolInfo(
            source="DOMINO_OFFLOAD",
            source_flight_id=offload.flight_id,
            eligible_for_no_show_replacement=True,
            priority_hold=(offload.status == "PRIORITY_HOLD"),
            disposition=offload.status or "AVAILABLE"
        )

    return result


# --- ENDPOINT 2: GET SINGLE WORK ITEM ---
@router.get("/api/v1/recovery-solutions/{work_item_id}", response_model=schemas.RecoverySolutionDetailResponse)
def get_recovery_solution_detail(work_item_id: str, db: Session = Depends(get_db)):
    workflow_control = get_workflow_control(db)
    
    # 1. Fetch the Offload Record (raise 404 if not found)
    offload = db.query(models.Offload).options(
        joinedload(models.Offload.uld).joinedload(models.Uld.loadtype_loadtype),
        joinedload(models.Offload.flight).joinedload(models.Flight.aircraft_aircraft)
    ).filter(models.Offload.offload_id == int(work_item_id)).first()
    
    if not offload:
        raise HTTPException(status_code=404, detail="Work item not found")

    # 2. Map to WorkItem (Pylance will now know this is a valid schemas.WorkItem)
    work_item: schemas.WorkItem = _map_offload_to_work_item(db, offload)

    # 3. Fetch Review History
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
            target_work_item_id=d.target_work_item_id,
            confirmed_no_show_event_id=d.confirmed_no_show_event_id,
            decision=d.decision,
            reason=d.reason or "No reason provided",
            reviewer_id=d.reviewer_id,
            reviewed_at=d.reviewed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if d.reviewed_at else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ))

    # 4. Construct Response
    response_dict = work_item.model_dump()
    response_dict["workflow_control"] = get_workflow_control(db)
    response_dict["review_history"] = history
    
    return response_dict

def _generate_real_plans(db: Session, offload: models.Offload) -> list[models.RecoveryPlan]:
    print(f"\n🔍 DEBUG: Generating options for Offload ID {offload.offload_id}")
    
    offloaded_uld = db.query(models.Uld).filter(models.Uld.uld_no == offload.uld_no).first()
    manifest = db.query(models.AwbUldFlight).filter(
        models.AwbUldFlight.uld_uld_no == offload.uld_no,
        models.AwbUldFlight.flight_flight_id == offload.flight_id
    ).first()
    
    if not manifest or not offloaded_uld:
        print("❌ DEBUG: Manifest or ULD not found.")
        return []
        
    awb = manifest.awb
    original_flight = db.query(models.Flight).filter(models.Flight.flight_id == offload.flight_id).first()
    
    if not original_flight or not original_flight.std or not original_flight.origin or not original_flight.dest or not awb.contract_ddl:
        print("❌ DEBUG: Missing core flight data or AWB deadline.")
        return []
        
    buffer_time = awb.contract_ddl - timedelta(hours=3)
    print(f"🕒 Original Flight: {original_flight.flight_id} | STD: {original_flight.std}")
    print(f"🕒 AWB Deadline: {awb.contract_ddl} | Buffer Time: {buffer_time}")
    
    candidate_flights = db.query(models.Flight).filter(
        models.Flight.origin == original_flight.origin,
        models.Flight.dest == original_flight.dest,
        models.Flight.std > original_flight.std,
        models.Flight.std < buffer_time,
        models.Flight.status.notin_(['CANCELLED'])
    ).order_by(models.Flight.std.asc()).all()

    print(f"✈️ Found {len(candidate_flights)} candidate flights before deadline.")
    for f in candidate_flights:
        print(f"   -> Candidate: {f.flight_id} | STD: {f.std}")

    plans = []
    offload_weight = float(manifest.weight or 0)
    if offloaded_uld.loadtype_loadtype:
        offload_weight += float(offloaded_uld.loadtype_loadtype.tare_weight_kg or 0)
    print(f"⚖️ Offload Weight (with tare): {offload_weight} kg")

    for flight in candidate_flights:
        if not flight.std: continue
            
        delay = int((flight.std - original_flight.std).total_seconds() / 60)
        
        booked_weight = sum(float(a.weight or 0) for a in flight.awb_uld_flight if a.weight)
        usable_capacity = float(flight.usable_capacity_kg or 0) if flight.usable_capacity_kg else 0.0
        if usable_capacity == 0.0 and flight.aircraft_aircraft:
            usable_capacity = float(flight.aircraft_aircraft.max_payload_weight or 0)
            
        remaining_capacity = usable_capacity - booked_weight
        print(f"   -> Flight {flight.flight_id}: Capacity {usable_capacity}, Booked {booked_weight}, Remaining {remaining_capacity}")
        
        if remaining_capacity >= offload_weight:
            print(f"      ✅ IDLE SPACE found on {flight.flight_id}")
            plan = models.RecoveryPlan(
                offload_id=offload.offload_id, plan_type='IDLE_SPACE',
                proposed_flight_id=flight.flight_id, status='Pending',
                feasibility_score=100.0, expected_delay_minutes=delay,
                total_modeled_expected_loss=float(awb.declared_value or 0) * (delay / 1440.0),
                notes=f"Direct rebook. Remaining capacity: {remaining_capacity:.0f} kg."
            )
            plans.append(plan)
        else:
            eligible_swap = [
                a for a in flight.awb_uld_flight 
                if a.awb and a.awb.priority_score is not None 
                and float(a.awb.priority_score) > float(awb.priority_score or 0)
                and float(a.weight or 0) >= (offload_weight - remaining_capacity)
            ]
            
            if eligible_swap:
                print(f"      🔄 SWAP possible on {flight.flight_id}")
                swap_target = sorted(eligible_swap, key=lambda x: float(x.awb.priority_score or 0), reverse=True)[0]
                plan = models.RecoveryPlan(
                    offload_id=offload.offload_id, plan_type='SWAP_DISPLACED',
                    proposed_flight_id=flight.flight_id, status='Pending',
                    feasibility_score=75.0, expected_delay_minutes=delay,
                    total_modeled_expected_loss=float(awb.declared_value or 0) * (delay / 1440.0) * 1.5,
                    swap_uld_no=swap_target.uld_uld_no,
                    notes=f"Requires swapping {swap_target.uld_uld_no} (Priority {swap_target.awb.priority_score})."
                )
                plans.append(plan)

    for p in plans:
        db.add(p)
    db.commit()
    return plans

# --- ENDPOINT 3: GET CANDIDATE ROUTING OPTIONS ---
@router.get("/api/v1/recovery-solutions/{work_item_id}/options", response_model=schemas.OptionsResponse)
def get_recovery_options(work_item_id: str, db: Session = Depends(get_db)):
    if not work_item_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid ID format.")
        
    offload = db.query(models.Offload).filter(models.Offload.offload_id == int(work_item_id)).first()
    if not offload:
        raise HTTPException(status_code=404, detail="Work item not found")
        
    if offload.source_type == 'Displaced':
        raise HTTPException(status_code=400, detail="Pool ULDs do not get routing options.")

    existing_plans = db.query(models.RecoveryPlan).filter(
        models.RecoveryPlan.offload_id == offload.offload_id
    ).order_by(models.RecoveryPlan.feasibility_score.desc()).all()

    if not existing_plans:
        existing_plans = _generate_real_plans(db, offload)

    if not existing_plans:
        raise HTTPException(status_code=404, detail="No recovery options found before the contractual deadline.")

    formatted_options = []
    best_options = []
    min_loss = float('inf')

    for plan in existing_plans:
        if plan.status == 'Discarded':
            continue
            
        flight = db.query(models.Flight).filter(models.Flight.flight_id == plan.proposed_flight_id).first()
        if not flight:
            continue
            
        loss = float(plan.total_modeled_expected_loss or 0)
        cost_breakdown = schemas.CostBreakdown(
            cargo_customer_impact=loss * 0.8,
            cathay_contribution_impact=loss * 0.2,
            recovery_and_handling_cost=1000.0,
            delay_and_service_impact=0.0,
            downstream_network_impact=0.0,
            total_modeled_expected_loss=loss,
            currency="HKD"
        )

        formatted_options.append(schemas.RoutingOption(
            option_id=str(plan.plan_id),
            option_type="rebook" if plan.plan_type == 'IDLE_SPACE' else "split",
            recovery_legs=[
                schemas.RecoveryLegInfo(**{"from": flight.origin or "UNK", "to": flight.dest or "UNK", "operating_flight_id": flight.flight_id})
            ],
            feasibility_status="Feasible",
            feasibility_percentage=int(plan.feasibility_score or 0),
            usable_capacity_kg=float(flight.usable_capacity_kg or 0),
            capacity_status="Confirmed",
            fuel_constraint="None",
            weather_exposure="Low",
            expected_arrival=flight.std.isoformat() + "Z" if flight.std else "",
            expected_delay_minutes=plan.expected_delay_minutes or 0,
            recovery_handling_cost=1000.0,
            total_modeled_expected_loss=loss,
            cost_breakdown=cost_breakdown,
            missing_data=[],
            network_impact=plan.notes or ""
        ))

        if plan.status == 'Pending' and loss < min_loss:
            min_loss = loss
            best_options = [str(plan.plan_id)]

    return {
        "work_item_id": work_item_id,
        "type": "DISRUPTION",
        "best_options": best_options,
        "options": formatted_options,
        "currency": "HKD"
    }

@router.get("/api/v1/recovery-solutions/{work_item_id}/options/{option_id}", response_model=schemas.OptionDetailResponse)
def get_option_detail(work_item_id: str, option_id: str, db: Session = Depends(get_db)):
    if not work_item_id.isdigit() or not option_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid ID format.")
        
    plan = db.query(models.RecoveryPlan).filter(models.RecoveryPlan.plan_id == int(option_id)).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Option not found")
        
    flight = db.query(models.Flight).filter(models.Flight.flight_id == plan.proposed_flight_id).first()
    offload = db.query(models.Offload).filter(models.Offload.offload_id == int(work_item_id)).first()
    
    if not flight or not offload:
        raise HTTPException(status_code=404, detail="Flight or Offload data missing")

    # Reconstruct RoutingOption
    loss = float(plan.total_modeled_expected_loss or 0)
    cost_breakdown = schemas.CostBreakdown(
        cargo_customer_impact=loss * 0.8,
        cathay_contribution_impact=loss * 0.2,
        recovery_and_handling_cost=1000.0,
        delay_and_service_impact=0.0,
        downstream_network_impact=0.0,
        total_modeled_expected_loss=loss,
        currency="HKD"
    )
    
    routing_option = schemas.RoutingOption(
        option_id=str(plan.plan_id),
        option_type="rebook" if plan.plan_type == 'IDLE_SPACE' else "split",
        recovery_legs=[
            schemas.RecoveryLegInfo(**{"from": flight.origin or "UNK", "to": flight.dest or "UNK", "operating_flight_id": flight.flight_id})
        ],
        feasibility_status="Feasible",
        feasibility_percentage=int(plan.feasibility_score or 0),
        usable_capacity_kg=float(flight.usable_capacity_kg or 0),
        capacity_status="Confirmed",
        fuel_constraint="None",
        weather_exposure="Low",
        expected_arrival=flight.std.isoformat() + "Z" if flight.std else "",
        expected_delay_minutes=plan.expected_delay_minutes or 0,
        recovery_handling_cost=1000.0,
        total_modeled_expected_loss=loss,
        cost_breakdown=cost_breakdown,
        missing_data=[],
        network_impact=plan.notes or ""
    )

    # Construct detailed explanations
    domino_effect = schemas.NetworkDominoEffect(
        affected_flights=[plan.proposed_flight_id],
        affected_uld_ids=[plan.swap_uld_no] if plan.swap_uld_no else [],
        affected_shipment_ids=[],
        additional_capacity_shortfall_kg=0.0,
        summary=plan.notes or "No downstream impact."
    )

    explanation = schemas.ExplanationInfo(
        summary=f"Route via {plan.plan_type} is currently optimal.",
        reasons=["Lowest modeled expected loss" if plan.plan_type == 'IDLE_SPACE' else "Capacity available via swap"],
        assumptions=["Station acceptance is required"],
        missing_data=[]
    )

    return schemas.OptionDetailResponse(
        work_item_id=work_item_id,
        option=routing_option,
        uld=schemas.ULDInfo(
            uld_id=offload.uld_no, uld_type="UNK", weight_kg=0.0, volume_m3=0.0, origin="HKG", destination="UNK"
        ),
        master_airway_bill=schemas.MAWBInfo(
            number="UNKNOWN", document_id="UNKNOWN", display_label="UNKNOWN"
        ), # You can expand this by fetching the actual AWB if needed
        cost_breakdown=cost_breakdown,
        network_domino_effect=domino_effect,
        scenario_sensitivity=[],
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
        return _map_offload_to_work_item(db, existing_event_offload)

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
        return _map_offload_to_work_item(db, offload)

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