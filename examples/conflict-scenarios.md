# Lock Conflict Scenarios

## Scenario 1: Advisory Locks Allow Overlap

Developer A starts an advisory session:
```bash
$ ats session start -s "frontend/**" -d "Updating Vue components"
Session started: abc123
  Locks created: 1 (advisory)
```

Developer B starts overlapping work (allowed):
```bash
$ ats session start -s "frontend/components/**" -d "Refactoring login form"
Session started: def456
  Locks created: 1 (advisory)
  ⚠️  Conflict notification sent to team
```

**Result**: Both sessions coexist. Team is notified to coordinate.

---

## Scenario 2: Exclusive Lock Blocks Overlap

Developer A starts exclusive session:
```bash
$ ats session start -s "backend/**" -d "Auth system refactor" --exclusive
Session started: ghi789
  Locks created: 1 (exclusive)
```

Developer B tries overlapping work (blocked):
```bash
$ ats session start -s "backend/auth/**" -d "Add OAuth"
Error 409: Cannot create session: scope 'backend/auth/**' conflicts
with exclusive lock 'backend/**' held by alice
```

**Result**: Developer B must wait or coordinate with Developer A.

---

## Scenario 3: Requesting Exclusive on Existing Advisory

Developer A has advisory lock:
```bash
$ ats session start -s "src/**" -d "General cleanup"
Session started: jkl012
```

Developer B requests exclusive (blocked):
```bash
$ ats session start -s "src/api/**" -d "API redesign" --exclusive
Error 409: Cannot create session: scope 'src/api/**' conflicts
with existing lock 'src/**' (you requested exclusive mode) held by alice
```

**Result**: Exclusive mode cannot coexist with any locks.

---

## Scenario 4: File-Level Lock Check

Before editing a specific file:
```bash
$ ats lock check backend/auth/middleware.py
  [WARNING] backend/auth/middleware.py — locked by alice (pattern: backend/**)
```

Exit code 1 indicates conflict detected.

---

## Best Practices

1. **Default to advisory** for normal development
2. **Use exclusive for**:
   - Database migrations
   - Breaking API changes
   - Major refactoring affecting many files
3. **Always check locks** before starting work: `ats lock check path/to/file`
4. **Check team status** regularly: `ats team`
5. **Log decisions** so teammates understand your choices
