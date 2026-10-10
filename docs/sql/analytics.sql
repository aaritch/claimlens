-- Run in the Glue database named by Terraform's analytics_database output.
-- Full snapshots repeat IDs. Use latest versions before aggregating.
CREATE OR REPLACE VIEW latest_validated_claims AS
SELECT id, doc_type, payer_name, provider_npi, service_date, disputed_amount,
       input_tokens, output_tokens, ocr_pages, cost_usd, elapsed_ms, version, updated_at
FROM (
    SELECT *, row_number() OVER (PARTITION BY id ORDER BY version DESC, updated_at DESC) AS rn
    FROM validated_claims
)
WHERE rn = 1;

SELECT payer_name, count(*) AS claims, avg(disputed_amount) AS average_dispute,
       sum(disputed_amount) AS total_disputed
FROM latest_validated_claims
GROUP BY payer_name
ORDER BY claims DESC;

SELECT doc_type, count(*) AS documents, sum(cost_usd) AS estimated_cost,
       avg(cost_usd) AS estimated_cost_per_document,
       approx_percentile(elapsed_ms, 0.95) AS latency_p95_ms
FROM latest_validated_claims
GROUP BY doc_type;

-- This table contains validated records only; it cannot report validation-failure rates.
-- Use CloudWatch ValidationFailures/Documents for routing rates.
