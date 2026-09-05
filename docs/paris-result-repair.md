# Paris result-method incident — 2026-09-05

Jose explicitly authorized direct production Mongo repair in the current task.
Scope: event 144513 / ESPN 600059993, every stored result on that card compared
against ESPN's explicit winning-method declaration. No prediction is edited.

The read-only plan found bouts 401911287 and 401914474 incorrectly recorded as
SUB instead of DEC. One scored pick gains one point. SUBMISSION TRIO ON THE CARD
must revert to ACTIVE at 1/3 and compensate its incorrect 6 XP award. SCORE
SIXTEEN becomes 5/16. Other selections and unplayed fights remain unchanged.

The repair script defaults to a read-only audit. Applying requires its current
plan hash, a matching event alias, matching winner and round, no conflicting
Admin override and an unchanged database snapshot after backup. It saves a
Fernet-encrypted recovery package and private key outside the repository under
the current user's `.codex/private/ufc-picks-repairs/<plan-id>` directory.
Neither credentials nor user documents are written into this report or logs.

Results, canonical revisions, durable Admin result commands, pick scores and
user statistics commit in one Mongo transaction. A stale result aborts every
primary mutation. Existing mission services then recalculate all assignments,
compensate the bad award, cancel its celebration and synchronize progression.
Repeated mission reconciliation uses the same revisions and is idempotent.

Recovery: a failure before the primary transaction commits leaves the database
unchanged. After commit, a mission failure is resumed with `--resume <plan-id>`;
this first verifies the corrected results have not changed and never reapplies
the primary writes. Keep the encrypted preimages for operator recovery. Do not
restore them blindly over newer live results; any manual restore requires a
fresh comparison and a separate exact recovery decision.

Verification: synthetic end-to-end coverage reproduces two false submissions,
reverses the mission completion, verifies one XP compensation and replay safety,
preserves the pick's fighter/method, and verifies full transaction rollback on a
stale second result. Production verification compares all stored card results
again and recomputes expected mission progress without writes.

The scraper code fix must also be integrated by Jose. Durable Admin commands
protect the corrected bouts from an older deployed scraper, but other future
results still need the fixed parser.
