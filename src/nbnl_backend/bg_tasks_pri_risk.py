# src/nbnl_backend/tasks.py
import random
import datetime
from sqlalchemy.orm import Session, joinedload
from src.nbnl_backend import models
from src.nbnl_backend.db import SessionLocal

# IATA Special Handling Codes that MUST NEVER be offloaded
PROTECTED_CODES = {'AVI', 'DGR'}  
# Cargo types that are highly sensitive to time/temperature
SENSITIVE_COMMODITIES = {'PER', 'PIL', 'PHR', 'FRESH', 'PHARMA'}

def calculate_awb_priority_score(awb: models.Awb) -> float:
    score = 100.0
    now = datetime.datetime.now()

    for shipment in awb.shipment:
        shc = shipment.special_handling_code.upper() if shipment.special_handling_code else ""
        comm_type = shipment.commodity_type.upper() if shipment.commodity_type else ""

        if shc in PROTECTED_CODES:
            return -1.0

        if shc == 'VAL':
            score -= 30.0

        if comm_type in SENSITIVE_COMMODITIES or shc in ['PIL', 'PER']:
            score -= 20.0
            break 

    if awb.contract_ddl:
        time_left_hours = (awb.contract_ddl - now).total_seconds() / 3600
        if time_left_hours < 6:
            score -= 50.0   
        elif time_left_hours < 24:
            score -= 30.0   
        elif time_left_hours < 48:
            score -= 10.0   

    if awb.declared_value and float(awb.declared_value) > 100000:
        score -= 15.0

    return max(score, 0.0)

def mock_ml_risk_score(awb_no: str, uld_no: str, flight_id: str) -> float:
    return round(random.uniform(0.0, 1.0), 2)

def run_batch_update_job():
    db: Session = SessionLocal()
    batch_size = 500
    updated_awb_count = 0
    updated_auf_count = 0

    try:
        # Update AWB Priority
        awb_query = db.query(models.Awb)\
            .join(models.Awb.awb_uld_flight)\
            .join(models.AwbUldFlight.flight_flight)\
            .options(joinedload(models.Awb.shipment))\
            .filter(models.Flight.std > datetime.datetime.now())\
            .distinct(models.Awb.awb_no)\
            .yield_per(batch_size)

        awb_updates = []
        for awb in awb_query:
            priority = calculate_awb_priority_score(awb)
            awb_updates.append({"awb_no": awb.awb_no, "priority_score": priority})

            if len(awb_updates) >= batch_size:
                db.bulk_update_mappings(models.Awb, awb_updates)
                db.commit()
                updated_awb_count += len(awb_updates)
                awb_updates = []

        if awb_updates:
            db.bulk_update_mappings(models.Awb, awb_updates)
            db.commit()
            updated_awb_count += len(awb_updates)

        print(f"[{datetime.datetime.now()}] Priority scores updated for {updated_awb_count} AWBs.")

        # Update Risk Scores
        auf_query = db.query(models.AwbUldFlight)\
            .join(models.Flight, models.AwbUldFlight.flight_flight_id == models.Flight.flight_id)\
            .filter(models.Flight.std > datetime.datetime.now())\
            .yield_per(batch_size)

        auf_updates = []
        for record in auf_query:
            risk = mock_ml_risk_score(record.awb_awb_no, record.uld_uld_no, record.flight_flight_id)
            auf_updates.append({
                "awb_awb_no": record.awb_awb_no,
                "uld_uld_no": record.uld_uld_no,
                "flight_flight_id": record.flight_flight_id,
                "offload_probability": risk
            })

            if len(auf_updates) >= batch_size:
                db.bulk_update_mappings(models.AwbUldFlight, auf_updates)
                db.commit()
                updated_auf_count += len(auf_updates)
                auf_updates = []

        if auf_updates:
            db.bulk_update_mappings(models.AwbUldFlight, auf_updates)
            db.commit()
            updated_auf_count += len(auf_updates)

        print(f"[{datetime.datetime.now()}] Risk scores updated for {updated_auf_count} AWB_ULD_FLIGHT records.")

    except Exception as e:
        db.rollback()
        print(f"Batch job failed: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    run_batch_update_job()