---
id: bench-t003
title: "Add null check to getUserDisplayName()"
difficulty: easy
bug_location: src/utils.ts
bug_type: missing-null-check
---

## Task

Add a null guard to `getUserDisplayName` in `src/utils.ts`.

## Current Behavior

```
getUserDisplayName({ id: 2, name: null, email: "t@x.com", age: 25 })
// → TypeError: Cannot read properties of null (reading 'toUpperCase')
```

## Expected Behavior

- When `user.name` is a non-null string, return the uppercased name.
- When `user.name` is `null`, return an empty string `""`.

## Verify

```bash
node --strip-types --test tests/utils.test.ts 2>&1 | grep -A2 "getUserDisplayName"
```

All `getUserDisplayName` describe block tests must pass.
