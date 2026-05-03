---
id: bench-t005
title: "Fix sort direction in sortByAge()"
difficulty: easy
bug_location: src/utils.ts
bug_type: wrong-comparator
---

## Task

Fix the comparator in `sortByAge` in `src/utils.ts` so it sorts ascending instead of descending.

## Current Behavior

```
sortByAge([{age:30}, {age:25}, {age:40}])
// → [{age:40}, {age:30}, {age:25}]  (descending — wrong)
```

## Expected Behavior

```
sortByAge([{age:30}, {age:25}, {age:40}])
// → [{age:25}, {age:30}, {age:40}]  (ascending — correct)
```

## Hint

The comparator `(a, b) => b.age - a.age` sorts descending. Swap `a` and `b` to get ascending.

## Verify

```bash
node --strip-types --test tests/utils.test.ts 2>&1 | grep -A2 "sortByAge"
```

All `sortByAge` describe block tests must pass.
