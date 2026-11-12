## 🚨 Cargo Disruption Scanner - Developer Guide & Testing
This document explains the logic behind the Disruption Scanner background task (bg_tasks_scanner.py) and provides a guide on how to test each case locally.

### 📖 Overview
The Disruption Scanner runs automatically every 5 minutes via APScheduler. Its sole purpose is to act as a "radar":

It scans the database for 10 specific disruption scenarios.

If a disruption is found, it creates a record in the offload table with source_type = 'Disrupted'.

If an ML prediction score exceeds the threshold, it creates a record with source_type = 'Predicted'.

Crucial: The scanner does not trigger the recovery plan. It only flags the problem and sets the status to 'Pending'. The agent will review it later.

### 🛠️ How to Test the Scanner Locally
1. Add the Execution Block
To test the scanner manually without waiting for the 5-minute timer, ensure your bg_tasks_scanner.py has this at the very bottom:

```
python
if __name__ == "__main__":
    run_disruption_scanner()
```
2. Run the Scanner via Docker
Open your terminal in the project root and run:
```
bash
docker exec -it cathay_cargo_api python -m src.nbnl_backend.bg_tasks_scanner
```
3. Verify the Output
Terminal Logs: You should see 🚨 STARTING DISRUPTION SCANNER... followed by ⚠️ ALERT: Flagged ULD... and finally ✅ Disruption scan complete.

Database (Supabase): Check the offload table. You should see a new row with the reason populated, source_type = 'Disrupted', and status = 'Pending'.

### 🧠 The Disruption Cases & How to Trigger Them
To test these, you will need to manually update your Supabase database to simulate the conditions.

#### Case 1: Extra Fuel Request
Logic: Checks if actual_fuel_weight > planned_fuel_weight. If the extra fuel causes the flight to exceed its usable cargo capacity, it triggers an offload.

How to Test:

```
sql
UPDATE flight 
SET actual_fuel_weight = planned_fuel_weight + 5000,
    usable_capacity_kg = 1000  -- Artificially make capacity very low
WHERE flight_id = 'FLT-CX880-1024';
```
Expected Result: The lowest priority ULD on CX880 is flagged.

#### Case 2: Excess Baggage (Passenger Flights)
Logic: Checks if actual_baggage_weight > planned_baggage_weight on is_pax = 'Y' flights.

How to Test:

```
sql
UPDATE flight 
SET actual_baggage_weight = 15000, 
    planned_baggage_weight = 10000
WHERE flight_id = 'FLT-CX250-1024';
```
Expected Result: Cargo on CX250 is flagged because the excess bags ate the cargo capacity.

#### Case 3: Missed Connection (MCT > 1 hour)
Logic: If an incoming flight arrives late (ata), it checks if any connecting cargo will miss the 1-hour Minimum Connection Time (MCT) for the next flight.

How to Test: (Ensure FLT-CX880-1024 and FLT-CX250-1024 share the same AWB).

```
sql
UPDATE flight 
SET ata = NOW(), std = NOW() - INTERVAL '2 hours' 
WHERE flight_id = 'FLT-CX880-1024';
```
Expected Result: The connecting ULD on FLT-CX250-1024 is flagged as Disrupted.

#### Case 4: Customer Request for Delay
Logic: If a customer puts a shipment on hold, it becomes a prime candidate to be bumped.

How to Test:

```
sql
UPDATE awb 
SET is_delayed_by_customer = TRUE 
WHERE awb_no = '160-11111111';
```
Expected Result: The ULD containing AWB 160-11111111 is flagged.

#### Case 5: ULD Not Found (Frontline Triggered)
Logic: Checks for ULDs where UldFlight.status = 'Missing'.

How to Test:

```
sql
UPDATE uld_flight 
SET status = 'Missing' 
WHERE uld_uld_no = 'PMC11111CX';
```
Expected Result: PMC11111CX is flagged, and the reason string includes its last seen location.

#### Case 6: Aircraft Change / Overbooking
Logic: Compares total booked cargo against the aircraft's maximum payload limit.

How to Test:

```
sql
-- Drastically lower the max payload of the aircraft
UPDATE aircraft 
SET max_payload_weight = 500 
WHERE aircraft_id = 'B-KAA';
```
Expected Result: The lowest priority ULD on CX880 is flagged for offload.

#### Case 7: Late Tender (Cut-off Missed)
Logic: Looks for flights departing in the next 90 minutes where cargo is still in 'Planned' status.

How to Test:

```
sql
UPDATE flight SET std = NOW() + INTERVAL '45 minutes' WHERE flight_id = 'FLT-CX880-1024';
UPDATE uld_flight SET status = 'Planned' WHERE uld_uld_no = 'PMC11111CX';
```
Expected Result: PMC11111CX is flagged for not showing up at the cut-off.

#### Case 8: Customs Hold
Logic: Checks for cargo stuck in customs.

How to Test:
```
sql
UPDATE awb_uld_flight 
SET status = 'Customs_Hold' 
WHERE awb_awb_no = '160-22222222';
```
Expected Result: The ULD containing AWB 160-22222222 is flagged.

#### Case 9: ULD Damaged
Logic: Checks if the physical ULD is broken.

How to Test:

```
sql
UPDATE uld SET condition = 'Damaged' WHERE uld_no = 'AKE12345CX';
```
Expected Result: AKE12345CX is flagged.

#### Case 10: Predictive Offload (ML Model Output)
Logic: If the Neural Network outputs an offload_probability >= 0.80, the ULD is flagged as a Predicted offload.

How to Test:
```
sql
UPDATE awb_uld_flight 
SET offload_probability = 0.85, 
    risk_reasons = '["Severe headwind expected"]'::jsonb
WHERE awb_awb_no = '160-33333333';
```
Expected Result: The ULD is flagged, but with source_type = 'Predicted'. The API will place this in the PREDICTED_OFFLOAD queue.


### ⚙️ Helper Logic (Under the Hood)
_check_capacity_and_flag
Used for Cases 1, 2, and 6. If the physical weight exceeds the capacity, it queries the manifest, finds all cargo, filters out any AWB with a ```priority_score``` = -1 (Protected/DG), and selects the ULD with the highest numerical priority score (lowest actual priority) to flag.

```_flag_specific_uld```
Used for Cases 3, 4, 5, 7, 8, 9.

It checks if the ULD is already in the offload table. If yes, it does nothing (prevents spam).

If not, it inserts a new row with ```source_type``` = 'Disrupted', ```status``` = 'Pending', and the reason.

### Deduplication
Because the scanner runs every 5 minutes, it relies on the ```existing = db.query(models.Offload).filter(...)``` check to prevent the agent's dashboard from being flooded with duplicate alerts for the same ULD.