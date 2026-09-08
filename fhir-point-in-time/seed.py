# /// script
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Seed one clinical story into XTDB as FHIR R4 resources.

The story, in clinical (valid) time on 2026-03-02:

    09:00  a serum potassium specimen is drawn from Ada Okafor
    09:15  the lab reports it as 5.9 mmol/L — hyperkalaemic, above the 5.0 ceiling
    09:30  a prescriber, reading that result, orders an insulin/dextrose infusion
    14:00  the lab amends the result: the specimen was haemolysed, and a repeat
           assay on the same sample reports 4.2 mmol/L, which is normal

The amendment is the point. Its *clinical* validity back-dates to the 09:00
specimen — the potassium was never 5.9 — but the *knowledge* of it only exists
from 14:00. So "what was the potassium at 09:30" and "what did the prescriber see
at 09:30" are different questions with different answers, and only the second one
defends the prescribing decision.

Valid time is ours to state, and is written explicitly. System time is XTDB's to
assign, one distinct instant per transaction, so the four writes below go in four
transactions and the seed asserts they came out strictly ordered.

Re-runnable: ERASE removes prior versions outright, so `_history` counts stay
honest across runs.

    uv run seed.py

Point it elsewhere with XTDB_DSN, e.g. at a dev REPL on port 5440.
"""

from __future__ import annotations

import os
import sys

import psycopg

DSN = os.environ.get("XTDB_DSN", "host=localhost port=5443 user=xtdb dbname=xtdb")

PATIENT = "Patient/pt-1"
OBS = "Observation/obs-k1"
MREQ = "MedicationRequest/mr-1"

SPECIMEN = "TIMESTAMP '2026-03-02T09:00:00Z'"
PRESCRIBED = "TIMESTAMP '2026-03-02T09:30:00Z'"

# CodeableConcept.text and Encounter.period are the two FHIR field names that
# collide with XTDB SQL keywords and must be quoted in a struct literal. Bare
# `{text: ...}` is a parse error; see README, known gaps.
POTASSIUM_CODE = """{
  "text": 'Potassium [Moles/volume] in Serum or Plasma',
  coding: [{system: 'http://loinc.org', code: '2823-3'}]
}"""


def mmol(value: float) -> str:
    return (
        f"{{value: {value}, unit: 'mmol/L', "
        f"system: 'http://unitsofmeasure.org', code: 'mmol/L'}}"
    )


def main() -> int:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        # Clean slate. First run has no tables yet, so tolerate the miss.
        for table, _id in (("patient", PATIENT), ("observation", OBS), ("medication_request", MREQ)):
            try:
                cur.execute(f"ERASE FROM {table} WHERE _id = '{_id}'")
            except psycopg.Error:
                pass

        # --- transaction 1: the patient ------------------------------------
        cur.execute(f"""
            INSERT INTO patient RECORDS {{
              _id: '{PATIENT}',
              resource_type: 'Patient',
              name_text: 'Ada Okafor',
              gender: 'female',
              birth_date: DATE '1962-04-11',
              _valid_from: TIMESTAMP '2026-01-01T00:00:00Z'
            }}
        """)

        # --- transaction 2: the lab result, as first reported --------------
        # _valid_from is the specimen time, not the filing time: FHIR's
        # Observation.effective[x] is the clinically relevant instant, which is
        # exactly the mapping xtdb/fhir-sandbox#3 asks for.
        cur.execute(f"""
            INSERT INTO observation RECORDS {{
              _id: '{OBS}',
              resource_type: 'Observation',
              status: 'final',
              subject_id: '{PATIENT}',
              code: {POTASSIUM_CODE},
              effective_date_time: {SPECIMEN},
              value_quantity: {mmol(5.9)},
              reference_range_high: 5.0,
              _valid_from: {SPECIMEN}
            }}
        """)

        # --- transaction 3: the prescription --------------------------------
        # The system time XTDB assigns here is the knowledge basis that defends
        # the decision: everything recorded up to this instant, and nothing after.
        cur.execute(f"""
            INSERT INTO medication_request RECORDS {{
              _id: '{MREQ}',
              resource_type: 'MedicationRequest',
              status: 'active',
              intent: 'order',
              subject_id: '{PATIENT}',
              authored_on: {PRESCRIBED},
              medication_codeable_concept: {{"text": 'Insulin/dextrose infusion'}},
              reason_reference: '{OBS}',
              _valid_from: {PRESCRIBED}
            }}
        """)

        # --- transaction 4: the amendment -----------------------------------
        # FOR PORTION OF VALID_TIME back-dates the correction to the specimen, so
        # it restates that one specimen and claims nothing about earlier time.
        # UPDATE patches rather than replaces, so code/subject_id/effective survive.
        cur.execute(f"""
            UPDATE observation
               FOR PORTION OF VALID_TIME FROM {SPECIMEN} TO NULL
               SET status = 'corrected',
                   value_quantity = {mmol(4.2)},
                   note = 'Specimen haemolysed; repeat assay on the same sample reports 4.2 mmol/L.'
             WHERE _id = '{OBS}'
        """)

        # The demo's whole claim rests on these being distinct and ordered. If a
        # future XTDB batched them into one transaction they would collapse to one
        # system time, the two axes would stop disagreeing, and the queries would
        # quietly return the same value twice. Fail loudly here instead.
        cur.execute(f"""
            SELECT _system_from FROM observation FOR ALL SYSTEM_TIME
             WHERE _id = '{OBS}' ORDER BY _system_from
        """)
        obs_times = [r[0] for r in cur.fetchall()]
        cur.execute(f"SELECT _system_from FROM medication_request WHERE _id = '{MREQ}'")
        prescribed_at = cur.fetchone()[0]

        if len(obs_times) != 2:
            print(f"expected 2 observation versions, got {len(obs_times)}", file=sys.stderr)
            return 1
        first_report, amendment = obs_times
        if not first_report < prescribed_at < amendment:
            print(
                "system times are not strictly ordered "
                f"(reported {first_report}, prescribed {prescribed_at}, amended {amendment})",
                file=sys.stderr,
            )
            return 1

        print("seeded one patient, one amended potassium result, one prescription\n")
        print("  knowledge basis            system time")
        print(f"  result first reported      {first_report.isoformat()}")
        print(f"  prescription written       {prescribed_at.isoformat()}   <- the basis that defends the decision")
        print(f"  amendment filed            {amendment.isoformat()}")
        print("\nnow run: uv run verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
