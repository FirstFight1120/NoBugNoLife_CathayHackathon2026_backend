import datetime
from decimal import Decimal
from sqlalchemy.orm import Session, joinedload
from src.nbnl_backend import models
from src.nbnl_backend.db import SessionLocal

def run_disruption_scanner():
    print("🚨 STARTING DISRUPTION SCANNER...")
    db: Session = SessionLocal()
    now = datetime.datetime.now()
    time_horizon = now + datetime.timedelta(hours=24)

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
                new_capacity = max(float(flight.usable_capacity_kg) - fuel_diff, 0.0)
                flight.usable_capacity_kg = Decimal(str(new_capacity))
                _check_capacity_and_flag(db, flight, "Extra fuel request exceeded usable capacity")

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
                new_capacity = max(float(flight.usable_capacity_kg) - bag_diff, 0.0)
                flight.usable_capacity_kg = Decimal(str(new_capacity))
                _check_capacity_and_flag(db, flight, "Excess baggage consumed cargo capacity")

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
        # CASE 4: Customer Request for Delay
        # =====================================================================
        delayed_awbs = db.query(models.Awb).filter(models.Awb.is_delayed_by_customer == True).all()
        for awb in delayed_awbs:
            for auf in awb.awb_uld_flight:
                if auf.flight_flight and auf.flight_flight.std and auf.flight_flight.std > now:
                    _flag_specific_uld(db, auf.uld_uld_no, auf.flight_flight_id, f"Customer delayed AWB {awb.awb_no}")

        # =====================================================================
        # CASE 5: ULD Not Found (Frontline Triggered)
        # =====================================================================
        missing_ulds = db.query(models.UldFlight).join(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.UldFlight.status == 'Missing' 
        ).all()
        for uf in missing_ulds:
            uld = db.query(models.Uld).filter(models.Uld.uld_no == uf.uld_uld_no).first()
            if uld:
                _flag_specific_uld(db, uld.uld_no, uf.flight_flight_id, f"ULD missing (Frontline reported). Last seen: {uld.current_location}")

        # =====================================================================
        # CASE 6: Aircraft Change / Overbooking
        # =====================================================================
        flights = db.query(models.Flight).filter(models.Flight.std > now, models.Flight.std < time_horizon).all()
        for flight in flights:
            if flight.aircraft_aircraft and flight.aircraft_aircraft.max_payload_weight is not None:
                max_payload = float(flight.aircraft_aircraft.max_payload_weight)
                booked_weight = sum(float(auf.weight or 0) for auf in flight.awb_uld_flight if auf.weight)
                if booked_weight > max_payload:
                    _check_capacity_and_flag(db, flight, "Aircraft change or overbooking exceeded max payload")

        # =====================================================================
        # CASE 7: Late Tender (Cut-off missed)
        # =====================================================================
        late_cargo = db.query(models.UldFlight).join(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < now + datetime.timedelta(minutes=90),
            models.UldFlight.status == 'Planned'
        ).all()
        for lc in late_cargo:
            _flag_specific_uld(db, lc.uld_uld_no, lc.flight_flight_id, "Late tender: not received at cut-off")

        # =====================================================================
        # CASE 8: Customs Hold
        # =====================================================================
        customs_held = db.query(models.AwbUldFlight).join(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.AwbUldFlight.status == 'Customs_Hold'
        ).all()
        for ch in customs_held:
            _flag_specific_uld(db, ch.uld_uld_no, ch.flight_flight_id, f"Customs hold on AWB {ch.awb_awb_no}")

        # =====================================================================
        # CASE 9: ULD Damaged
        # =====================================================================
        damaged_ulds = db.query(models.UldFlight).join(models.Uld).join(models.Flight).filter(
            models.Flight.std > now, models.Flight.std < time_horizon,
            models.Uld.condition == 'Damaged'
        ).all()
        for du in damaged_ulds:
            _flag_specific_uld(db, du.uld_uld_no, du.flight_flight_id, f"ULD {du.uld_uld_no} is damaged")


        # =====================================================================
        # CASE 10: Predictive Offload (ML Model Output)
        # =====================================================================
        # Threshold defined as 0.80 (80%)
        PREDICTION_THRESHOLD = 0.80
        
        high_risk_cargo = db.query(models.AwbUldFlight).join(models.Flight).filter(
            models.Flight.std > now,
            models.Flight.std < time_horizon,
            models.AwbUldFlight.offload_probability >= PREDICTION_THRESHOLD,
            models.AwbUldFlight.status != 'Offloaded' # Ignore already offloaded cargo
        ).all()

        for hr in high_risk_cargo:
            # Check if this specific ULD is ALREADY flagged in the Offload table
            existing_offload = db.query(models.Offload).filter(
                models.Offload.uld_no == hr.uld_uld_no,
                models.Offload.flight_id == hr.flight_flight_id
            ).first()

            # If not already flagged, create a 'Predicted' offload record
            if not existing_offload:
                reason_str = f"High risk prediction ({float(hr.offload_probability or 0)*100}%). "
                if hr.risk_reasons and isinstance(hr.risk_reasons, list) and len(hr.risk_reasons) > 0:
                    reason_str += str(hr.risk_reasons[0])

                print(f"🔮 PREDICTION ALERT: Flagging ULD {hr.uld_uld_no} on Flight {hr.flight_flight_id} (Prob: {hr.offload_probability})")
                
                new_offload = models.Offload(
                    uld_no=hr.uld_uld_no,
                    flight_id=hr.flight_flight_id,
                    source_type='Predicted', # Crucial: This places it in the PREDICTED_OFFLOAD queue
                    reason=reason_str,
                    status='Pending'
                )
                db.add(new_offload)

        db.commit()
        print("✅ Disruption scan complete.")

    except Exception as e:
        db.rollback()
        print(f"❌ DISRUPTION SCANNER FAILED: {e}")
    finally:
        db.close()


def _check_capacity_and_flag(db: Session, flight: models.Flight, reason: str):
    if flight.usable_capacity_kg is None:
        return
        
    booked_weight = sum(float(auf.weight or 0) for auf in flight.awb_uld_flight if auf.weight)
    
    if booked_weight > float(flight.usable_capacity_kg):
        
        eligible = [
            m for m in flight.awb_uld_flight 
            if m.awb and m.awb.priority_score is not None and m.awb.priority_score != -1
        ]
        if eligible:
            sorted_manifest = sorted(eligible, key=lambda x: float(x.awb.priority_score or 0), reverse=True)
            target_uld = sorted_manifest[0].uld_uld_no
            _flag_specific_uld(db, target_uld, flight.flight_id, reason)


def _flag_specific_uld(db: Session, uld_no: str, flight_id: str, reason: str):
    existing = db.query(models.Offload).filter(
        models.Offload.uld_no == uld_no,
        models.Offload.flight_id == flight_id
    ).first()
    
    if existing:
        return 

    print(f"⚠️ ALERT: Flagged ULD {uld_no} on Flight {flight_id}. Reason: {reason}")
    new_offload = models.Offload(
        uld_no=uld_no,
        flight_id=flight_id,
        source_type='Disrupted',
        reason=reason,
        status='Pending'
    )
    db.add(new_offload)