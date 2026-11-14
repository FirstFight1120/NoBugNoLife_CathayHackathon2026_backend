import datetime
from decimal import Decimal
from sqlalchemy.orm import Session, joinedload
from src.nbnl_backend import models
from src.nbnl_backend.db import SessionLocal

def run_disruption_scanner():
    print("🚨 STARTING DISRUPTION SCANNER...")
    db: Session = SessionLocal()
    now = datetime.datetime.now()
    time_horizon = now + datetime.timedelta(days=7) 

    try:
        # =====================================================================
        # CASE 1: Extra Fuel Request
        # =====================================================================
        flights_fuel = db.query(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.Flight.actual_fuel_weight > models.Flight.planned_fuel_weight
        ).all()

        for flight in flights_fuel:
            fuel_diff = float(flight.actual_fuel_weight or 0) - float(flight.planned_fuel_weight or 0)
            if flight.usable_capacity_kg is not None:
                # Calculate effective capacity dynamically (do not permanently mutate the DB)
                effective_capacity = max(float(flight.usable_capacity_kg) - fuel_diff, 0.0)
                _check_capacity_and_flag(db, flight, "Extra fuel request exceeded usable capacity", effective_capacity)

        # =====================================================================
        # CASE 2: Excess Baggage
        # =====================================================================
        flights_baggage = db.query(models.Flight).join(models.Aircraft).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.Aircraft.is_pax == 'Y',
            models.Flight.actual_baggage_weight > models.Flight.planned_baggage_weight
        ).all()

        for flight in flights_baggage:
            bag_diff = float(flight.actual_baggage_weight or 0) - float(flight.planned_baggage_weight or 0)
            if flight.usable_capacity_kg is not None:
                effective_capacity = max(float(flight.usable_capacity_kg) - bag_diff, 0.0)
                _check_capacity_and_flag(db, flight, "Excess baggage consumed cargo capacity", effective_capacity)

        # =====================================================================
        # CASE 3: Missed Connection (MCT > 1 hour)
        # =====================================================================
        late_flights = db.query(models.Flight).filter(
            models.Flight.ata.isnot(None),
            models.Flight.ata > models.Flight.std + datetime.timedelta(hours=1)
        ).all()
        
        for late_flight in late_flights:
            connecting_auf = db.query(models.AwbUldFlight).filter(
                models.AwbUldFlight.flight_flight_id == late_flight.flight_id
            ).all()
            for auf in connecting_auf:
                next_auf = db.query(models.AwbUldFlight).filter(
                    models.AwbUldFlight.awb_awb_no == auf.awb_awb_no,
                    models.AwbUldFlight.flight_flight_id != auf.flight_flight_id
                ).first()
                if next_auf:
                    next_flight = db.query(models.Flight).filter(models.Flight.flight_id == next_auf.flight_flight_id).first()
                    if next_flight and next_flight.std and late_flight.ata and late_flight.ata + datetime.timedelta(hours=1) > next_flight.std:
                        _flag_specific_uld(db, next_auf.uld_uld_no, next_flight.flight_id, "Missed connection due to prior delay")

        # =====================================================================
        # CASE 6: Aircraft Change / Overbooking / Equipment Change
        # =====================================================================
        flights = db.query(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon
        ).all()
        
        for flight in flights:
            # Priority 1: Use the calculated usable capacity (accounts for equipment change, fuel, baggage)
            capacity_limit = 0.0
            if flight.usable_capacity_kg is not None and float(flight.usable_capacity_kg) > 0:
                capacity_limit = float(flight.usable_capacity_kg)
            # Fallback: Use aircraft max payload if capacity is missing
            elif flight.aircraft_aircraft and flight.aircraft_aircraft.max_payload_weight is not None:
                capacity_limit = float(flight.aircraft_aircraft.max_payload_weight)
                
            if capacity_limit > 0:
                _check_capacity_and_flag(db, flight, "Aircraft change exceeded usable capacity", capacity_limit)

        # =====================================================================
        # CASE 10: Flight Cancelled
        # =====================================================================
        cancelled_flights = db.query(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.Flight.status == 'CANCELLED'
        ).all()

        for flight in cancelled_flights:
            for auf in flight.awb_uld_flight:
                _flag_specific_uld(db, auf.uld_uld_no, flight.flight_id, "Flight Cancelled: All cargo must be re-routed")

        # =====================================================================
        # CASE 12: Predictive Offload (ML Model Output)
        # =====================================================================
        PREDICTION_THRESHOLD = 0.80
        
        high_risk_cargo = db.query(models.AwbUldFlight).join(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.AwbUldFlight.offload_probability >= PREDICTION_THRESHOLD,
            models.AwbUldFlight.status != 'Offloaded',
            models.Flight.status.notin_(['CANCELLED']) # Only exclude cancelled flights
        ).all()

        for hr in high_risk_cargo:
            # _flag_specific_uld will automatically skip it if it's already Disrupted
            prob_percentage = float(hr.offload_probability or 0) * 100
            reason_str = f"High risk prediction ({prob_percentage:.1f}%). "
            if hr.risk_reasons and isinstance(hr.risk_reasons, list) and len(hr.risk_reasons) > 0:
                reason_str += str(hr.risk_reasons[0])

            _flag_specific_uld(db, hr.uld_uld_no, hr.flight_flight_id, reason_str, source_type='Predicted')

        db.commit()
        print("✅ Disruption scan complete.")

    except Exception as e:
        db.rollback()
        print(f"❌ DISRUPTION SCANNER FAILED: {e}")
    finally:
        db.close()


def _check_capacity_and_flag(db: Session, flight: models.Flight, reason: str, capacity_kg: float = 0.0):
    """
    Checks capacity shortfall and offloads multiple low-priority ULDs 
    until the booked weight is brought under the capacity limit.
    """
    if capacity_kg is None:
        capacity_kg = float(flight.usable_capacity_kg) if flight.usable_capacity_kg is not None else 0.0
        
    # 1. Find ULDs that are ALREADY flagged in the offload table so we don't count them
    already_offloaded_ulds = db.query(models.Offload.uld_no).filter(
        models.Offload.flight_id == flight.flight_id
    ).all()
    offloaded_uld_nos = {u[0] for u in already_offloaded_ulds}
    
    # 2. Calculate booked weight ONLY from ULDs that are NOT yet offloaded
    booked_weight = sum(
        float(auf.weight or 0) 
        for auf in flight.awb_uld_flight 
        if auf.weight and auf.uld_uld_no not in offloaded_uld_nos
    )
    
    shortfall = booked_weight - capacity_kg
    
    if shortfall > 0:
        print(f"📉 Capacity Shortfall on {flight.flight_id}: {shortfall:.2f} kg. Finding ULDs to offload...")
        
        # 3. Filter out protected cargo (-1) and ULDs that are already offloaded
        eligible = [
            m for m in flight.awb_uld_flight 
            if m.awb and m.awb.priority_score is not None 
            and m.awb.priority_score != -1
            and m.uld_uld_no not in offloaded_uld_nos # Skip already offloaded
        ]
        
        # Sort by priority (highest score = lowest actual priority)
        sorted_manifest = sorted(eligible, key=lambda x: float(x.awb.priority_score or 0), reverse=True)
        
        freed_weight = 0.0
        for target in sorted_manifest:
            if freed_weight >= shortfall:
                break # We have freed enough weight to resolve the shortfall
                
            net_weight = float(target.weight or 0)
            tare_weight = 0.0
            
            if target.uld and target.uld.loadtype_loadtype:
                tare_weight = float(target.uld.loadtype_loadtype.tare_weight_kg or 0)
                
            freed_weight += (net_weight + tare_weight)
            
            _flag_specific_uld(db, target.uld_uld_no, flight.flight_id, reason)


def _flag_specific_uld(db: Session, uld_no: str, flight_id: str, reason: str, source_type='Disrupted'):
    """Inserts an offload record. Upgrades to Disrupted if a real issue occurs."""
    existing = db.query(models.Offload).filter(
        models.Offload.uld_no == uld_no,
        models.Offload.flight_id == flight_id
    ).first()
    
    if existing:
        # If it was a Prediction, but is now a real Disruption, upgrade it!
        if existing.source_type == 'Predicted' and source_type == 'Disrupted':
            print(f"🔄 UPGRADING ULD {uld_no} from PREDICTED to DISRUPTED. Reason: {reason}")
            existing.source_type = 'Disrupted'
            existing.reason = reason
            existing.status = 'Pending'
        return 

    print(f"⚠️ ALERT: Flagged ULD {uld_no} on Flight {flight_id}. Reason: {reason}")
    new_offload = models.Offload(
        uld_no=uld_no,
        flight_id=flight_id,
        source_type=source_type,
        reason=reason,
        status='Pending'
    )
    db.add(new_offload)
    db.flush() # Force flush so subsequent cases see it immediately