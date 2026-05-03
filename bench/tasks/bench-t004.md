---
id: bench-t004
title: "Fix logic error in isEligibleForDiscount()"
difficulty: medium
bug_location: src/utils.ts
bug_type: wrong-logical-operator
---

## Task

Fix the logical operator in `isEligibleForDiscount` in `src/utils.ts`.

## Business Rule

A user is eligible for a discount **only when BOTH** conditions are met:
1. The user is 65 years of age or older
2. The cart has more than 5 items

## Current Behavior

The function uses `||` (OR), so any senior citizen or anyone with a large cart qualifies — too permissive.

## Expected Behavior

| user.age | cart.items.length | eligible? |
|----------|-------------------|-----------|
| 70       | 6                 | true      |
| 30       | 6                 | false     |
| 70       | 3                 | false     |
| 30       | 3                 | false     |

## Verify

```bash
node --strip-types --test tests/utils.test.ts 2>&1 | grep -A2 "isEligibleForDiscount"
```

All `isEligibleForDiscount` describe block tests must pass.
