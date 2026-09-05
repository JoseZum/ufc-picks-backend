# Public mission history

`GET /missions/users/{user_id}` now includes `history`, using the same
`SelectedMissionView` entries and ordering as the owner's `/missions/profile`.
Each settled entry includes its assignment ID, name, description, difficulty,
status, event label and earned XP. Completed, failed and void missions are
included; active missions and private celebrations remain excluded.

The existing `recent` field remains the latest eight completed missions for
older clients. `history` uses the existing profile query's limit of 200 recent
assignments, so it is not an unbounded lifetime archive. Authentication and
mission feature access checks are unchanged.

The frontend shares one filterable history component across the owner's hub
and both public profile frames (dialog and page). It falls back to `recent`
when connected to an older backend. Each row expands to show its description.
