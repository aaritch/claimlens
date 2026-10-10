# ADR 004: Promote models and prompts through evaluation, not intuition

**Status:** accepted.

Model IDs/prices are configurable. Classification and extraction can use different models.
Use forced tool output and Pydantic, then deterministic validation. Promote changes based on measured F1,
auto-accept precision, error catch rate, cost and regressions on fixed synthetic data.

Consequences: offline mock CI is a plumbing regression check only. Real Bedrock evaluation is explicit and billed,
uses protected short-lived credentials, and must pass before the deployment workflow proceeds.
Use a separate held-out seed after iteration; regenerate current AWS prices rather than trusting placeholders.
