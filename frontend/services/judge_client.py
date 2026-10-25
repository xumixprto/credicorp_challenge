from typing import Literal

from frontend.services.api_client import APIClient
from shared.evaluations import EvaluationTable, JudgeResponse


class JudgeClient(APIClient):
    target_name = "judge"
    timeout_message = (
        "The evaluation request timed out. Some batches may still complete. "
        "Refresh the table before evaluating missing runs again."
    )

    def table(self) -> EvaluationTable:
        return self._request("GET", "/judge", EvaluationTable.model_validate, params={"limit": 50})

    def download_csv(self, table_name: Literal["runs", "evaluations"]) -> bytes:
        def validate(payload: object) -> bytes:
            if not isinstance(payload, bytes):
                raise ValueError("Invalid CSV response")
            return payload

        return self._request("GET", f"/judge/export/{table_name}", validate, decode_json=False)

    def evaluate(self, run_ids: list[str] | None = None) -> JudgeResponse:
        return self._request(
            "POST",
            "/judge",
            JudgeResponse.model_validate,
            json={"run_ids": run_ids, "missing_only": run_ids is None},
        )
