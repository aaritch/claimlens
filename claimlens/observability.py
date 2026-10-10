"""CloudWatch Embedded Metric Format, via existing logs, without additional AWS calls or permissions."""
from __future__ import annotations

import json
import math
import sys
import time
from collections.abc import Callable

from claimlens.models import ProcessingResult

METRICS = {
    "Documents": "Count", "InputTokens": "Count", "OutputTokens": "Count",
    "CostUSD": "None", "Latency": "Milliseconds", "OCRDocuments": "Count",
    "OCRPages": "Count", "ValidationFailures": "Count", "ProcessingErrors": "Count",
}


class Metrics:
    def __init__(self, environment: str = "dev", enabled: bool = False,
                 sink: Callable[[str], None] | None = None):
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", environment):
            raise ValueError("invalid metrics environment")
        self.environment, self.enabled = environment, enabled
        self.sink = sink or (lambda line: print(line, file=sys.stdout, flush=True))

    def record(self, result: ProcessingResult) -> None:
        if not self.enabled:
            return
        values = {
            "Documents": 1, "InputTokens": result.usage.input_tokens,
            "OutputTokens": result.usage.output_tokens, "CostUSD": result.usage.cost_usd,
            "Latency": result.elapsed_ms, "OCRDocuments": int(result.ocr is not None),
            "OCRPages": result.usage.ocr_pages,
            "ValidationFailures": int(result.status in {"needs_review", "rejected"}),
            "ProcessingErrors": int(result.status == "error"),
        }
        if any(not math.isfinite(v) or v < 0 for v in values.values()):
            return
        payload = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "ClaimLens", "Dimensions": [["Environment"]],
                    "Metrics": [{"Name": name, "Unit": unit} for name, unit in METRICS.items()],
                }],
            },
            "Environment": self.environment,
            **values,
        }
        try:
            self.sink(json.dumps(payload))
        except Exception:
            # Telemetry must never turn a successful document into a retried model bill.
            pass
