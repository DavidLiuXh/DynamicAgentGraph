from typing import Literal

from pydantic import model_validator

from ..graph.schemas import ContractModel
from ..graph.spec import GraphSpec


class PlanningDiagnostic(ContractModel):
    reason_code: Literal["MISSING_INFORMATION", "CAPABILITY_GAP", "CONSTRAINT_CONFLICT"]
    message: str
    related_input_paths: list[str]
    missing_information: list[str]
    required_capability_description: list[str]


class PlanningResponse(ContractModel):
    response_version: Literal["1.0"]
    outcome: Literal["graph", "blocked"]
    graph: GraphSpec | None
    diagnostics: list[PlanningDiagnostic]

    @model_validator(mode="after")
    def validate_branch(self):
        if self.outcome == "graph" and (self.graph is None or self.diagnostics):
            raise ValueError("graph outcome requires graph and no diagnostics")
        if self.outcome == "blocked" and (self.graph is not None or not self.diagnostics):
            raise ValueError("blocked outcome requires diagnostics and no graph")
        return self
