# Dashboard and Source Health Repair

## Goal

Restore the Source Monitor dashboard and make source-health reporting accurately distinguish a successful empty response from a failed fetch. Repair invalid source configuration that is reproducible from the daily run, while keeping temporary upstream outages non-fatal and visible.

## Scope

### Dashboard

`docs/health.html` must use the deployed Supabase configuration for every request. The Source Monitor query will use the already configured Supabase client instead of separate, hard-coded credentials. A request error will show its Supabase error message in the monitor status area.

### Source health

Fetcher metadata must retain request failures even when a fetcher returns an empty list after logging an HTTP error. The orchestration health state will then increase the failure count and expose the source as degraded in notifications and the health dashboard.

An empty but successful source response remains distinct from a failed source request.

### Adapter remediation

The implementation will verify the JSearch request contract and update it only to the current supported form. It will validate the configured Lever boards for Atlassian and Netflix and the Workday configuration for Walmart; invalid configurations will be corrected when a current public board is available, otherwise disabled or removed rather than continuously failing.

Remotive is an external dependency. Its fetcher will make a bounded retry for transient server/gateway failures and return an explicit source failure if it still cannot retrieve results.

Missing company board URLs are data-quality defects only. They will be populated where a canonical public board can be confirmed; they must not be represented as fetch failures.

## Data flow

1. A browser session creates the configured Supabase client from `supabase-config.js`.
2. Source Monitor reads `company_source_health` through that client and renders returned data, an empty state, or a useful error state.
3. Each fetch adapter returns jobs plus a reliable failure signal to orchestration.
4. Orchestration writes source health, sends notifications, and continues processing healthy sources even when an individual source fails.

## Error handling

* No API key or service-role key is exposed to the dashboard.
* A failed source cannot abort the full daily pipeline.
* Retried upstream failures retain the final error for the health system.
* Source configuration is changed only after confirming the current endpoint or board identifier.

## Tests and verification

* Add a regression test proving Source Monitor has no hard-coded Supabase credentials and reads through the configured client.
* Add health-state/orchestration tests that a fetch failure increments failure state while an empty successful result does not.
* Add focused adapter tests for corrected JSearch, Lever, Workday, and Remotive behavior.
* Run the targeted tests, then the complete test suite and lint checks. Manually smoke-test the deployed dashboard query against the configured Supabase project.

## Non-goals

* Rebuilding the dashboard UI.
* Treating normal zero-job results as failures.
* Guaranteeing availability of third-party job boards or the Remotive API.
