---
id: bench-t001
title: "Fix off-by-one error in paginate()"
difficulty: easy
bug_location: src/utils.ts
bug_type: off-by-one
---

## Task

Fix the off-by-one error in the `paginate` function in `src/utils.ts`.

## Current Behavior

`paginate([1,2,3,4,5,6,7,8,9,10], 1, 3)` returns `[1, 2, 3, 4]` (4 items instead of 3).

## Expected Behavior

`paginate(items, page, pageSize)` must return exactly `pageSize` items per page.

## Hint

Look at how `end` is calculated from `start` and `pageSize`.

## Verify

```bash
node --strip-types --test tests/utils.test.ts 2>&1 | grep -A2 "paginate"
```

All `paginate` describe block tests must pass.
