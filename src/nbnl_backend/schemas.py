from pydantic import BaseModel, ConfigDict
from typing import List, Optional

def to_camel(string: str) -> str:
    components = string.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

class ForecastRisk(CamelModel):
    offload_probability: float
    capacity_shortfall_kg: Optional[float] = 0.0
    reasons: List[str]

class PreventionPlanItem(CamelModel):
    flight_id: str
    target_uld_no: str       # Added for ULD-level offload
    at_risk_awbs: List[str]  # Added to show which AWBs triggered the risk
    status: str
    risk_level: str
    risk_types: List[str]
    offload_probability: float
    predicted_offload_rate: float
    affected_shipment_count: int
    forecast_risk: ForecastRisk
    value_at_risk: float
    currency: str = "HKD"

class Pagination(CamelModel):
    page: int
    total_pages: int
    has_next_page: bool

class PreventionPlanResponse(CamelModel):
    items: List[PreventionPlanItem]
    total_cases: int
    total_shipments_at_risk: int
    pagination: Pagination