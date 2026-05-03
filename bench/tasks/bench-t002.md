---
id: bench-t002
title: "Fix calculation error in calculateTotal()"
difficulty: easy
bug_location: src/utils.ts
bug_type: wrong-operator
---

## Task

Fix the arithmetic error in the `calculateTotal` function in `src/utils.ts`.

## Current Behavior

For a cart with `[{quantity: 2, price: 10}, {quantity: 3, price: 5}]`:
- Returns `20` (adds quantity + price per item: 2+10 + 3+5)
- Should return `35` (multiplies quantity * price: 2*10 + 3*5)

## Expected Behavior

`calculateTotal(items)` must multiply each item's `quantity` by its `price`, then sum all results.

## Verify

```bash
node --strip-types --test tests/utils.test.ts 2>&1 | grep -A2 "calculateTotal"
```

All `calculateTotal` describe block tests must pass.
