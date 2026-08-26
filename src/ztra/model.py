"""Base class for every schema model: unknown fields are an error."""

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
