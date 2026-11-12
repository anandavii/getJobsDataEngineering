# Plan 08 — Rejected Jobs Airtable View

**Date:** 2026-03-04
**Status:** Approved

## Problem

The orchestrator already produces two categories of rejected jobs per run:
- `rejected_non_qa_jobs` — failed the QA relevance filter (not a software QA role)
- `rejected_low_fit_jobs` — passed QA filter but scored below the fit threshold

Both are saved to `.tmp/` but never delivered anywhere. The user has no way to review them
to tune filters or catch missed matches.

## Goal

Push all rejected jobs to a dedicated, web-accessible Airtable table after every run,
with the same familiar interface as the existing qualified jobs view.

## Decision: Separate Airtable Base

A new base **"Job Monitor - Rejected"** is used instead of adding a table to the existing
"Job Monitor" base. Rationale:

- Free plan gives **1,000 records per base** shared across all tables.
- Existing base (Jobs, Applications, Students) must not be polluted by high-volume rejections.
- Separate base isolates record budgets and cleanup policies.

## Airtable Table: "Rejected Jobs"

| Field              | Airtable Type      | Notes                                                                 |
|--------------------|--------------------|-----------------------------------------------------------------------|
| Job URL            | URL                | Upsert key — deduplication anchor                                    |
| Job Title          | Single line text   |                                                                       |
| Company            | Single line text   |                                                                       |
| Location           | Single line text   |                                                                       |
| Job Type           | Single select      | Remote / Hybrid / On-site / Unknown                                  |
| Source             | Single line text   | e.g. jsearch, greenhouse, lever                                      |
| Rejection Type     | Single select      | Not QA / Low Fit Score                                               |
| Rejection Reason   | Single line text   | Specific code from qa_relevance or role_horizon                      |
| Fit Score          | Number (decimal)   | Populated for Low Fit Score rows; blank for Not QA                   |
| Date Found         | Date               | Auto-set to today on insert                                          |
| Date Posted        | Date               | From source, may be blank                                            |
| Description Snippet| Long text          | First ~500 chars of job description                                  |

### Single Select Colours (configure manually in Airtable)
- `Rejection Type`: Not QA → red, Low Fit Score → orange
- `Job Type`: Remote → green, Hybrid → yellow, On-site → blue, Unknown → grey

## Retention Policy

3-day cleanup window (vs 60-day for qualified jobs).

**Steady-state math:** ~150 rejections/run × 2 runs/day × 3 days = ~900 records ✓ within 1,000 cap.

## New Env Var & Secret

| Key                         | Where                          |
|-----------------------------|--------------------------------|
| `AIRTABLE_REJECTED_BASE_ID` | `.env` (local) + GitHub Secret |

## Code Changes

### 1. `tools/push_to_airtable.py`
- Add optional `table_name: str` parameter (default `"Jobs"`) so the same module handles
  both qualified and rejected pushes.
- Add optional `cleanup_days: int` parameter (default `60`) to allow shorter retention for
  rejected jobs.
- Add `_to_rejected_airtable_fields()` mapper that includes `Rejection Type`, `Rejection Reason`,
  and `Fit Score` fields absent from the qualified mapper.

### 2. `tools/orchestrate.py`
- Read `AIRTABLE_REJECTED_BASE_ID` from env.
- After the existing qualified jobs push, merge `rejected_non_qa_jobs` and
  `rejected_low_fit_jobs` into a single list, annotating each with `rejection_type`
  (`Not QA` / `Low Fit Score`).
- Call `push_to_airtable.run()` with the rejected base ID, `table_name="Rejected Jobs"`,
  and `cleanup_days=3`.
- Log the result; do not exit on rejected-push failures (non-critical path).

### 3. `.env` (local)
- Add `AIRTABLE_REJECTED_BASE_ID=<new base ID>`

### 4. `.github/workflows/daily_job_search.yml`
- Add `AIRTABLE_REJECTED_BASE_ID: ${{ secrets.AIRTABLE_REJECTED_BASE_ID }}` to the env block.

## Setup Steps (Manual, One-Time)

1. Create a new Airtable base named **"Job Monitor - Rejected"**.
2. Create a table named **"Rejected Jobs"** with the fields above.
3. Copy the new base ID (starts with `app`) → add to `.env` and GitHub Secrets.

## Out of Scope

- No UI changes to the existing Jobs table or qualified jobs flow.
- No changes to filter logic or scoring — this is purely a delivery change.
- No read-back or sync from the Rejected Jobs table into the pipeline.
