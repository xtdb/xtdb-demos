# /// script
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Assert the three beats of the FHIR point-in-time demo.

Content assertions, not "did it throw" — every check names the clinical value it
expects, because the whole claim of the demo is that two queries over the same
instant disagree, and a smoke test that only catches exceptions would wave that
through.

Exits non-zero on any unexpected result, so it works as a CI gate.

    uv run verify.py

Point it elsewhere with XTDB_DSN, e.g. at a dev REPL on port 5440.
"""

from __future__ import annotations

import os
import sys

import psycopg

DSN = os.environ.get("XTDB_DSN", "host=localhost port=5443 user=xtdb dbname=xtdb")

OBS = "Observation/obs-k1"
MREQ = "MedicationRequest/mr-1"

# The clinical timeline the seed writes. Valid time is ours to state; system time
# is XTDB's to assign, so it is discovered at run time rather than hardcoded.
SPECIMEN = "TIMESTAMP '2026-03-02T09:00:00Z'"  # specimen drawn
PRESCRIBED = "TIMESTAMP '2026-03-02T09:30:00Z'"  # prescriber acts
BEFORE_SPECIMEN = "TIMESTAMP '2026-03-02T08:00:00Z'"  # nothing known yet

WRONG = 5.9  # what the lab first reported: hyperkalaemic
RIGHT = 4.2  # the amendment: haemolysed sample, actually normal

failures: list[str] = []
checks = 0


def check(name: str, got, want) -> None:
    global checks
    checks += 1
    if got == want:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}\n          got  {got!r}\n          want {want!r}")
        failures.append(name)


def one(cur, sql: str):
    cur.execute(sql)
    row = cur.fetchone()
    return row[0] if row else None


def main() -> int:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        # The knowledge basis at the moment of prescribing: the system time XTDB
        # assigned to the transaction that recorded the prescription. Derived, not
        # hardcoded — and fetched into Python because a subquery cannot be a
        # temporal bound (see README, known gaps).
        basis = one(cur, f"SELECT _system_from FROM medication_request WHERE _id = '{MREQ}'")
        if basis is None:
            print("no seed data found — run `uv run seed.py` first", file=sys.stderr)
            return 2
        at_prescribing = f"TIMESTAMP '{basis.isoformat()}'"

        print("\nBeat 1 — vread and _history, without a shadow history table")

        # FHIR `_history` on the instance: every version the server ever held.
        versions = one(cur, f"""
            SELECT COUNT(*) FROM observation FOR ALL SYSTEM_TIME WHERE _id = '{OBS}'
        """)
        check("_history returns both versions", versions, 2)

        # FHIR `vread`: the version current at a given knowledge basis.
        check(
            "vread at the prescriber's basis returns the pre-amendment value",
            one(cur, f"""
                SELECT (value_quantity).value FROM observation
                FOR SYSTEM_TIME AS OF {at_prescribing}
                WHERE _id = '{OBS}'
            """),
            WRONG,
        )
        check(
            "status was 'final' then, 'corrected' now",
            (
                one(cur, f"SELECT status FROM observation FOR SYSTEM_TIME AS OF {at_prescribing} WHERE _id = '{OBS}'"),
                one(cur, f"SELECT status FROM observation WHERE _id = '{OBS}'"),
            ),
            ("final", "corrected"),
        )

        print("\nBeat 2 — the two axes disagree about the same instant")

        # Same valid time, two knowledge bases. This pair is the demo.
        as_known_then = one(cur, f"""
            SELECT (value_quantity).value FROM observation
            FOR VALID_TIME  AS OF {PRESCRIBED}
            FOR SYSTEM_TIME AS OF {at_prescribing}
            WHERE _id = '{OBS}'
        """)
        as_known_now = one(cur, f"""
            SELECT (value_quantity).value FROM observation
            FOR VALID_TIME AS OF {PRESCRIBED}
            WHERE _id = '{OBS}'
        """)
        check("what the prescriber saw at 09:30", as_known_then, WRONG)
        check("what is now known to have been true at 09:30", as_known_now, RIGHT)
        check("the two axes disagree", as_known_then != as_known_now, True)

        # The correction is bounded by the specimen time: it restates the specimen,
        # it does not claim to know anything before the sample was drawn.
        check(
            "no potassium is asserted before the specimen was drawn",
            one(cur, f"""
                SELECT COUNT(*) FROM observation
                FOR VALID_TIME AS OF {BEFORE_SPECIMEN}
                WHERE _id = '{OBS}'
            """),
            0,
        )
        check(
            "the amendment back-dates to the specimen, not to when it was filed",
            one(cur, f"""
                SELECT (value_quantity).value FROM observation
                FOR VALID_TIME AS OF {SPECIMEN}
                WHERE _id = '{OBS}'
            """),
            RIGHT,
        )

        print("\nBeat 3 — the chart as the prescriber saw it, across resource types")

        # One statement, three resource types, both axes pinned. This is the query
        # FHIR's own history model cannot express: `_history` and `vread` are
        # per-resource-instance, so a client must walk every version chain itself.
        cur.execute(f"""
            SETTING DEFAULT SYSTEM_TIME AS OF {at_prescribing}
            SELECT p.name_text        AS patient,
                   (o.value_quantity).value AS potassium,
                   o.status           AS obs_status,
                   (m.medication_codeable_concept)."text" AS medication
              FROM patient FOR VALID_TIME AS OF {PRESCRIBED} p
              JOIN observation FOR VALID_TIME AS OF {PRESCRIBED} o
                ON o.subject_id = p._id
              JOIN medication_request FOR VALID_TIME AS OF {PRESCRIBED} m
                ON m.subject_id = p._id
        """)
        chart = cur.fetchall()
        check("the prescriber's chart is one row", len(chart), 1)
        if chart:
            check(
                "it shows the patient, the wrong potassium, and the drug given for it",
                chart[0],
                ("Ada Okafor", WRONG, "final", "Insulin/dextrose infusion"),
            )

        # And the same query at current knowledge tells the other story.
        cur.execute(f"""
            SELECT (o.value_quantity).value, o.status
              FROM observation FOR VALID_TIME AS OF {PRESCRIBED} o
             WHERE o._id = '{OBS}'
        """)
        check("at current knowledge the same chart reads differently", cur.fetchone(), (RIGHT, "corrected"))

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
