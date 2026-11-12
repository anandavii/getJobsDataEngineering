# Fix CodeRabbit Review Issues

## Issue 1: False positive "us" pattern in location filter (orchestrate.py)

**Problem:** The `_USA` frozenset includes bare `"us"` which is checked via substring match (`"us" in loc`). This causes false positives for non-US locations containing "us" (e.g., "Brussels", "Australia", "Russia", "Lausanne").

**Fix:** Remove `"us"` from the frozenset. Add a regex word-boundary check (`\bus\b`) for standalone "US" as a country code. The existing patterns (`"usa"`, `"u.s."`, `"u.s.a"`, `"united states"`, state abbreviations) already cover the vast majority of US locations.

**Changes:**
- `tools/orchestrate.py`: Add `import re` at the top, remove `"us"` from `_USA`, add `re.search(r'\bus\b', loc)` check in `_keep()`.

## Issue 2: Unbounded recursion in `_find_joblist_data` (fetch_jobs_dice.py)

**Problem:** The recursive helper traverses parsed RSC payload without a depth limit. While unlikely to hit Python's recursion limit in practice, a malformed payload could cause a stack overflow.

**Fix:** Add a `max_depth` parameter (default 50) that decrements on each recursive call and returns `[]` when exhausted.

**Changes:**
- `tools/fetch_jobs_dice.py`: Add `max_depth: int = 50` parameter, decrement on recursion, early return at 0.
