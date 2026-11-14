from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional
from datetime import datetime

# Base CamelModel (assuming you already have this)
class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=lambda s: ''.join([s.split('_')[0]] + [w.title() for w in s.split('_')[1:]]), populate_by_name=True)

# --- Workflow Control ---
class WorkflowControl(CamelModel):
    active_priority: Optional[int]
    can_work_disruptions: bool
    can_work_predictions: bool
    can_review_pool: bool
    allowed_actions_by_type: dict
    blocking_work_item_ids: List[str]
    message: Optional[str]

# --- Sub-objects for Work Items ---
class ULDInfo(CamelModel):
    uld_id: str
    uld_type: str
    weight_kg: float
    volume_m3: float
    origin: str
    destination: str

class MAWBInfo(CamelModel):
    number: str
    document_id: str
    display_label: str

class FlightInfo(CamelModel):
    flight_id: str
    origin: str
    destination: str
    scheduled_departure: str
    aircraft_type: Optional[str]

class CargoInfo(CamelModel):
    shipment_ids: List[str]
    weight_kg: float
    volume_m3: float
    handling_requirements: List[str]
    commodity: Optional[str]
    value_at_risk: Optional[float]
    currency: Optional[str]

class DocumentInfo(CamelModel):
    document_id: str
    version: int
    updated_at: str

class ForecastInfo(CamelModel):
    offload_probability: float
    capacity_shortfall_kg: float
    confidence: int
    forecasted_at: str
    reasons: List[str]
    forecast_cargo_capacity_kg: float
    booked_weight_kg: float
    value_at_risk: float
    currency: str

class ConfirmedOffload(CamelModel):
    confirmed_at: str
    source: str # OPERATIONAL_EVENT or AUTHORIZED_DECISION
    source_event_id: str
    reason: str

class CaseSummary(CamelModel):
    disruption: str
    capacity_constraint: str
    delivery_commitment: str
    baseline_expected_loss: float
    currency: str

class PoolInfo(CamelModel):
    source: str # DOMINO_OFFLOAD
    source_flight_id: str
    eligible_for_no_show_replacement: bool
    priority_hold: bool
    disposition: str # AVAILABLE, PRIORITY_HOLD, NO_SHOW_REVIEW

# --- Main Work Item Response ---
class WorkItem(CamelModel):
    work_item_id: str
    type: str
    status: str
    prediction_unit: str = "ULD"
    uld: ULDInfo
    master_airway_bill: MAWBInfo
    flight: FlightInfo
    cargo: CargoInfo
    document: DocumentInfo
    updated_at: str
    
    # Type-specific fields
    confirmed_offload: Optional[ConfirmedOffload] = None
    case_summary: Optional[CaseSummary] = None
    forecast: Optional[ForecastInfo] = None
    pool: Optional[PoolInfo] = None

class FlightSummary(CamelModel):
    flight_id: str
    origin: str
    destination: str
    scheduled_departure: str
    aircraft_type: Optional[str]
    offload_probability: float
    capacity_shortfall_kg: float
    forecast_cargo_capacity_kg: float
    booked_weight_kg: float
    affected_uld_count: int
    shipment_ids: List[str]
    value_at_risk: float
    currency: str
    confidence: int
    forecasted_at: str
    reasons: List[str]

class Pagination(CamelModel):
    page: int
    total_pages: int
    has_next_page: bool

class RecoverySolutionListResponse(CamelModel):
    items: List[WorkItem]
    total_items: int
    total_by_type: dict
    pagination: Pagination
    workflow_control: WorkflowControl
    flight_summaries: Optional[List[FlightSummary]] = []


class ReviewHistoryEntry(CamelModel):
    decision_id: int
    type: str
    uld_id: str
    master_airway_bill_number: str
    document_version: int
    option_id: Optional[str] = None
    target_work_item_id: Optional[str] = None
    confirmed_no_show_event_id: Optional[str] = None
    decision: str
    reason: str
    reviewer_id: str
    reviewed_at: str

class RecoverySolutionDetailResponse(CamelModel):
    # Common WorkItem fields flattened into the response
    work_item_id: str
    type: str
    status: str
    prediction_unit: str = "ULD"
    uld: ULDInfo
    master_airway_bill: MAWBInfo
    flight: FlightInfo
    cargo: CargoInfo
    document: DocumentInfo
    updated_at: str
    
    # Type specific fields
    confirmed_offload: Optional[ConfirmedOffload] = None
    case_summary: Optional[CaseSummary] = None
    forecast: Optional[ForecastInfo] = None
    pool: Optional[PoolInfo] = None
    
    # Detail specific fields
    workflow_control: WorkflowControl
    review_history: List[ReviewHistoryEntry]

class RecoveryLegInfo(CamelModel):
    # Using alias for "from" since it's a reserved keyword in Python
    from_loc: str = Field(alias="from") 
    to_loc: str = Field(alias="to")
    operating_flight_id: str

class CostBreakdown(CamelModel):
    cargo_customer_impact: float
    cathay_contribution_impact: float
    recovery_and_handling_cost: float
    delay_and_service_impact: float
    downstream_network_impact: float
    total_modeled_expected_loss: float
    currency: str

class RoutingOption(CamelModel):
    option_id: str
    option_type: str # rebook, reroute, wait_for_next_flight, split
    recovery_legs: List[RecoveryLegInfo]
    feasibility_status: str # Feasible, Infeasible, Feasibility Unknown
    feasibility_percentage: int
    usable_capacity_kg: float
    capacity_status: str # Confirmed, Nominal / Weather Constrained, Capacity Confirmation Pending
    fuel_constraint: str
    weather_exposure: str
    expected_arrival: str
    expected_delay_minutes: int
    recovery_handling_cost: float
    total_modeled_expected_loss: float
    cost_breakdown: CostBreakdown
    missing_data: List[str]
    network_impact: str

class OptionsResponse(CamelModel):
    work_item_id: str
    type: str
    best_options: List[str]
    options: List[RoutingOption]
    currency: str

class NetworkDominoEffect(CamelModel):
    affected_flights: List[str]
    affected_uld_ids: List[str]
    affected_shipment_ids: List[str]
    additional_capacity_shortfall_kg: float
    summary: str

class ScenarioSensitivity(CamelModel):
    input_change: str
    feasibility_status: str
    cost_breakdown: CostBreakdown

class ExplanationInfo(CamelModel):
    summary: str
    reasons: List[str]
    assumptions: List[str]
    missing_data: List[str]

class OffloadConfirmationRequest(CamelModel):
    # All fields have defaults so Swagger UI works instantly
    reason: str = "Agent confirmed manual offload due to operational constraints."
    source: str = "AUTHORIZED_DECISION"
    reviewer_id: str = "ops-agent-01"
    
    # These will be auto-generated by the backend if left blank
    source_event_id: Optional[str] = None 
    confirmed_at: Optional[str] = None 
    document_version: Optional[int] = None

class DecisionRequest(CamelModel):
    type: str # DISRUPTION, PREDICTED_OFFLOAD, PENDING_POOL
    uld_id: str
    master_airway_bill_number: str
    option_id: Optional[str] = None
    target_work_item_id: Optional[str] = None
    confirmed_no_show_event_id: Optional[str] = None
    decision: str # APPROVE_FOR_REVIEW, REJECT_OVERRIDE, HOLD_FOR_PRIORITY, etc.
    reason: str
    
    # Mocked fields for simplified testing
    reviewer_id: str = "mock-agent-01"
    reviewed_at: Optional[str] = None # Auto-generated by backend if left blank

class DecisionResponse(CamelModel):
    decision_id: int
    work_item_id: str
    type: str
    uld_id: str
    master_airway_bill_number: str
    decision: str
    target_work_item_id: Optional[str]
    reviewed_at: str