# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.



Design Decisions

Keep the existing FastAPI structure. The application already exposes the required employee, attendance, analytics, health, and explain endpoints. Retaining the existing structure minimizes regression risk and keeps the implementation aligned with the supplied tests.

Use MongoDB indexes that match access patterns. Unique employee and attendance keys protect data integrity. Supporting indexes should reflect the filters and sort orders used by the API. Actual execution plans and statistics should be checked before concluding that an index is effective.

Return JSON-compatible explain output. MongoDB explain responses can contain BSON-specific values. Convert them safely before returning the response so clients receive valid JSON while useful plan and execution information remains available.

Preserve explicit attendance rules. Timestamp conversion, IST shift-date assignment, late-arrival and overtime thresholds, work-hour rounding, weekday calculations, and regularization validation should follow the assignment specification exactly. Avoid changing edge-case behavior without a failing test or other concrete evidence.

Prefer small, tested changes. The current test baseline is 28 passing tests. Keep the working implementation stable, document genuine defects and their fixes, and add regression tests when correcting a confirmed issue. A successful HTTP status or passing test suite is useful evidence, but neither alone proves every execution plan or assignment requirement has been verified.