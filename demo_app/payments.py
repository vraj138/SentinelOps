"""Payments service: the end of the request chain. Returns a fake charge."""

import uuid

from pydantic import BaseModel, Field

from demo_app.common import DemoSettings, create_app

settings = DemoSettings()
app = create_app("payments", settings)


class ChargeRequest(BaseModel):
    order_id: int
    amount_cents: int = Field(gt=0)


class ChargeResponse(BaseModel):
    charge_id: str
    order_id: int
    status: str


@app.post("/charge")
def charge(request: ChargeRequest) -> ChargeResponse:
    """Pretend to charge a card and return a charge ID."""
    return ChargeResponse(
        charge_id=f"ch_{uuid.uuid4().hex[:12]}", order_id=request.order_id, status="succeeded"
    )
