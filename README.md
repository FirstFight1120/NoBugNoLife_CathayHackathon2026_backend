### Case 1: Extra Fuel Request (Capacity Drop)
Trigger Query: Flights departing in the next 24 hours where actual_fuel_weight > planned_fuel_weight.

Business Logic:

Calculates the difference: fuel_diff = actual - planned.

Subtracts this from the flight's usable_capacity_kg. (It uses max(new_capacity, 0) to ensure it never drops below zero, preventing math errors).

Compares the new capacity against the total booked_weight of all ULDs on the flight.

Action: If booked weight > new capacity, it flags the lowest priority ULD on the flight.

### Case 2: Excess Baggage (Passenger Flights)
Trigger Query: Flights where is_pax == 'Y' and actual_baggage_weight > planned_baggage_weight.

Business Logic:

Calculates the difference: bag_diff = actual - planned.

Subtracts this from the cargo capacity. (Passenger bags get priority over cargo in the belly hold).

Checks if the remaining capacity is now less than the booked cargo weight.

Action: Flags the lowest priority ULD on the flight.

### Case 3: Missed Connection (Transshipment)
Trigger Query: Flights that have already arrived where actual_arrival_time (ata) > scheduled_departure_time (std) + 1 hour. (This means the incoming flight was heavily delayed).

Business Logic:

Looks at all cargo on that late incoming flight.

For each AWB, it finds the next connecting flight.

Checks the Minimum Connection Time (MCT): If incoming_ata + 1 hour > outgoing_flight.std, the cargo cannot physically make the connection.

Action: Flags the specific ULD on the outgoing flight so it can be offloaded and re-routed.

### Case 4: Customer Request for Delay
Trigger Query: AWBs where is_delayed_by_customer == True (This is a manual flag set by the customer service team).

Business Logic: If a customer says "don't fly my cargo yet," the system finds the upcoming flight for that AWB.

Action: It immediately flags the ULD holding that AWB. Because the customer delayed it, the priority_score will naturally be very high (lowest priority), making it the perfect candidate to offload to free up space for someone else.

### Case 5: ULD Not Found (Missing)
Trigger Query: ALL future flights
where status == 'Missing'.

Business Logic: Checks the uld.status

Action: Flags the ULD and creates an alert. The reason string includes the last_scanned_location (e.g., "ULD missing. Last seen: HKG Ramp 4") so frontline staff know where to start looking.

### Case 6: Aircraft Change / Overbooking
Trigger Query: All future flights.

Business Logic: Compares the sum of all booked_weight on the flight against the aircraft.max_payload_weight.

Action: If booked weight > max payload (e.g., a last-minute downgrade from a 777 to an A330, or severe overbooking), it triggers a capacity check and flags the lowest priority ULD.

### Case 7: Late Tender of Cargo (Cut-off Missed)
Trigger Query: Flights departing in the next 90 minutes where the ULD status is still 'Planned'.

Business Logic: If the flight is leaving in 90 minutes and the ULD hasn't been scanned as "Received" or "Loaded" yet, it means the forwarder missed the cargo cut-off time.

Action: Flags the ULD. The agent can then use the pending pool to find waitlisted cargo to fill the empty space.

### Case 8: Customs Hold
Trigger Query: Cargo on future flights where AwbUldFlight.status == 'Customs_Hold'.

Business Logic: Customs is holding the cargo. If it isn't released soon, it will miss the flight.

Action: Flags the ULD. (The agent can decide whether to wait for customs or offload it immediately to avoid delaying the flight's departure).

### Case 9: ULD Damaged / Unserviceable
Trigger Query: ULDs assigned to future flights where uld.condition == 'Damaged'.

Business Logic: A damaged ULD cannot be loaded onto an aircraft for safety reasons.

Action: Flags the ULD immediately. This requires urgent repacking of the cargo into a new ULD.

### Case 10: Flow Control / ATC Restrictions (Needs Agent Review)
Trigger Query: Flights with a status of 'Flow_Controlled', 'ATC_Restricted', or 'Delayed'.

Business Logic: Flow control doesn't always mean cargo must be offloaded. It just means the flight is delayed.

Action: It runs the capacity check and flags the lowest priority ULD, but marks the offload as status='Pending'. The agent is alerted to review the flight. If the delay is minor, the agent clicks "Waive" (and the cargo flies normally). If the delay is severe and causes a weight/fuel issue, the agent clicks "Confirm" to proceed with the offload.