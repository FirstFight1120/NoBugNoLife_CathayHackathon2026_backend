from pydantic import BaseModel, ConfigDict
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

class PoolInfo(CamelModel):
    source: str
    source_flight_id: str
    eligible_for_no_show_replacement: bool
    priority_hold: bool
    disposition: str

# --- Main Work Item Response ---
class WorkItem(CamelModel):
    work_item_id: str
    type: str # DISRUPTION, PREDICTED_OFFLOAD, PENDING_POOL
    status: str
    prediction_unit: str = "ULD"
    uld: ULDInfo
    master_airway_bill: MAWBInfo
    flight: FlightInfo
    cargo: CargoInfo
    document: DocumentInfo
    updated_at: str
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

# --- Action / Decisions ---
class DecisionRequest(CamelModel):
    type: str
    uld_id: str
    master_airway_bill_number: str
    document_version: int
    option_id: Optional[str] = None
    decision: str # APPROVE_FOR_REVIEW, REJECT_OVERRIDE, etc.
    reason: str
    reviewer_id: str
    reviewed_at: str
    target_work_item_id: Optional[str] = None
    confirmed_no_show_event_id: Optional[str] = None