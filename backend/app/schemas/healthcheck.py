from typing import Literal

from pydantic import BaseModel


class HealthcheckResponse(BaseModel):
    status: Literal["ok"] = "ok"
