# FHIR point-in-time

FHIR specifies a version history in exhaustive detail and then makes it optional.
From the spec's own [versioning section](http://hl7.org/fhir/R4/http.html#versions):

> "For this reason, servers are allowed to not provide versioning support and this
> API does not enforce that versioning is supported."

Full version support is a SHOULD, not a SHALL, and `no-version` is a conformant
`CapabilityStatement.rest.resource.versioning` value. So servers that do keep
history hand-roll it: HAPI has `HFJ_RES_VER`, Aidbox writes a shadow
`<resource>_history` table per resource type plus a global sequence to order them.

This demo does that job in SQL, and then does the one thing FHIR's own history
model structurally cannot: reconstruct a chart **across resource types** as of a
past instant. `_history` and `vread` are per-resource-instance, so "what did this
patient's record look like at 09:30" is something a client has to assemble by
walking every version chain itself.

## The story

One patient, one amended lab result, one prescription, in clinical time on
2026-03-02:

| | |
| --- | --- |
| 09:00 | a serum potassium specimen is drawn from Ada Okafor |
| 09:15 | the lab reports 5.9 mmol/L — hyperkalaemic, above the 5.0 ceiling |
| 09:30 | a prescriber, reading that result, orders an insulin/dextrose infusion |
| 14:00 | the lab amends it: specimen haemolysed, a repeat assay reports 4.2 — normal |

The amendment is the point. Its *clinical* validity back-dates to the 09:00
specimen, because the potassium was never 5.9. But the *knowledge* of it only
exists from 14:00. So these are different questions with different answers:

- what was the potassium at 09:30? **4.2** — that is what the specimen held
- what did the prescriber see at 09:30? **5.9** — and that is what defends the decision

Only the second one answers the question a malpractice review asks, and it needs
both axes. `meta.lastUpdated` is single-axis: it records when the row changed,
never when the fact was true.

## Run it

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d          # XTDB on :5443
uv run seed.py                # one patient, one amendment, one prescription
uv run verify.py              # 11 assertions across the three beats
```

Tear down with `docker compose down -v`. `seed.py` is re-runnable: it `ERASE`s
its three resources first, so `_history` counts stay honest across runs.

XTDB's host port is **5443**, not 5432, for the reason `bikeshare-grafana`
documents: anything already bound to 5432 wins it silently on macOS, and you get
a seeded node and an empty node with nothing to tell you why.

## The three beats

### 1. `vread` and `_history`, without a shadow history table

Every version the server ever held, which is FHIR's instance-level `_history`:

```sql
SELECT (value_quantity).value, status, _system_from
  FROM observation FOR ALL SYSTEM_TIME
 WHERE _id = 'Observation/obs-k1'
```

Two rows: `5.9 / final` and `4.2 / corrected`. FHIR's `vread`, the version current
at a knowledge basis, is `FOR SYSTEM_TIME AS OF <basis>`. One clause each, against
the ordinary table.

### 2. The two axes disagree about the same instant

This pair is the demo. Same valid time, two knowledge bases:

```sql
-- what the prescriber saw at 09:30
SELECT (value_quantity).value FROM observation
FOR VALID_TIME  AS OF TIMESTAMP '2026-03-02T09:30:00Z'
FOR SYSTEM_TIME AS OF <the prescribing transaction>
WHERE _id = 'Observation/obs-k1'                        -- 5.9

-- what is now known to have been true at 09:30
SELECT (value_quantity).value FROM observation
FOR VALID_TIME AS OF TIMESTAMP '2026-03-02T09:30:00Z'
WHERE _id = 'Observation/obs-k1'                        -- 4.2
```

The correction is bounded, not a rewrite. It is written with `UPDATE ... FOR
PORTION OF VALID_TIME FROM <specimen> TO NULL`, so it restates that one specimen
and asserts nothing about time before the sample was drawn — reading at 08:00
returns no row at all, at either basis.

That shape is what HIPAA's amendment rule already describes. 45 CFR 164.526 says
an accepted amendment is made by "appending, or otherwise providing a link to, the
location of the amendment", not by overwriting the original.

### 3. The chart as the prescriber saw it, across resource types

```sql
SETTING DEFAULT SYSTEM_TIME AS OF <the prescribing transaction>
SELECT p.name_text                        AS patient,
       (o.value_quantity).value           AS potassium,
       o.status                           AS obs_status,
       (m.medication_codeable_concept)."text" AS medication
  FROM patient FOR VALID_TIME AS OF TIMESTAMP '2026-03-02T09:30:00Z' p
  JOIN observation FOR VALID_TIME AS OF TIMESTAMP '2026-03-02T09:30:00Z' o
    ON o.subject_id = p._id
  JOIN medication_request FOR VALID_TIME AS OF TIMESTAMP '2026-03-02T09:30:00Z' m
    ON m.subject_id = p._id
```

One statement, three resource types, both axes pinned: `Ada Okafor, 5.9, final,
Insulin/dextrose infusion`. Drop the `SETTING` line and the same chart reads
`4.2, corrected`.

This is the beat with no FHIR equivalent. There is no cross-resource `_history`,
and `_at` applies per interaction, so composing a coherent point-in-time snapshot
over several resources is left to the client on every read.

## How FHIR maps onto the two axes

| FHIR | XTDB | why |
| --- | --- | --- |
| `Observation.effective[x]` | `_valid_from` | the clinically relevant instant, not the filing time |
| `meta.lastUpdated` / `versionId` | `_system_from` | server-assigned; when the record changed |
| `_history` (instance) | `FOR ALL SYSTEM_TIME` | every version held |
| `vread` | `FOR SYSTEM_TIME AS OF` | the version current at a basis |
| `status: corrected` + back-dated fact | `UPDATE ... FOR PORTION OF VALID_TIME` | a bounded restatement |

Mapping `effective[x]` onto valid time is what
[xtdb/fhir-sandbox#3](https://github.com/xtdb/fhir-sandbox/issues/3) asks for; that
repo currently has the mapping written but commented out, and is a load-testing
harness rather than a FHIR server.

## Versions

Pinned to **XTDB 2.1.0**, the current stable release. Everything here is in it:
`FOR VALID_TIME AS OF`, `FOR SYSTEM_TIME AS OF`, `FOR ALL SYSTEM_TIME`, `SETTING
DEFAULT SYSTEM_TIME AS OF`, `INSERT ... RECORDS`, `UPDATE ... FOR PORTION OF
VALID_TIME`, `ERASE`, and nested struct reads. No pre-release needed.

## Verify it

```bash
uv run verify.py
```

11 assertions across the three beats, all on content rather than on the absence of
exceptions — the claim is that two queries disagree, and a check that only catches
throws would wave that through. Exits non-zero on any unexpected result, so it
works as a CI gate. `seed.py` additionally asserts that the four writes landed in
strictly ordered, distinct system times; if a future release batched them into one
transaction the axes would silently stop disagreeing.

## Known gaps

Verified against 2.1.0. All XTDB-side.

**`text` and `period` can't be bare struct keys or field accessors.**

```sql
INSERT INTO t RECORDS {_id: 'a', code: {text: 'K'}}   -- no viable alternative at input
SELECT (code).text FROM t                              -- same
```

Quoting works, in both positions: `{"text": 'K'}` and `(code)."text"`. This bites
FHIR harder than most schemas, because `CodeableConcept.text` appears on nearly
every coded field and `Period` is a core datatype (`Encounter.period`,
`Observation.effectivePeriod`). Of 14 common FHIR field names tested — `text`,
`code`, `system`, `value`, `unit`, `status`, `display`, `note`, `period`, `start`,
`end`, `reference`, `name`, `type` — only `text` and `period` fail bare.

**A subquery can't be a temporal bound.**

```sql
SELECT status FROM observation FOR SYSTEM_TIME AS OF
  (SELECT MIN(_system_from) FROM observation FOR ALL SYSTEM_TIME)
-- Errors planning SQL statement
```

So a knowledge basis can't be named by derivation ("as first recorded"). `seed.py`
and `verify.py` fetch the timestamp into Python and interpolate it as a literal.
Same gap `bikeshare-grafana` documents on 2.2.0-rc0.

## Demo rough edges

- **System time is real, so it is seed wall-clock, not the clinical 09:15 and
  14:00.** Valid time is ours to state and is written explicitly; system time is
  database-assigned, which is precisely what makes it trustworthy for audit. The
  narrative times are therefore the valid-time timeline, and "the prescribing
  basis" is discovered from `_system_from` at run time rather than hardcoded.
- Three resources and one correction. Enough to make the point, nowhere near a
  realistic corpus — [xtdb/fhir-sandbox](https://github.com/xtdb/fhir-sandbox)
  generates Synthea data at volume if you want load.
- The resources are a faithful-shaped subset, not valid FHIR JSON: snake_cased
  columns, `subject_id` as a plain string rather than a `Reference`, and no
  `meta`, `identifier`, or `Provenance`. Nothing validates against a
  StructureDefinition.
- **This is a storage substrate, not a FHIR server.** There is no REST layer, no
  `CapabilityStatement`, no SMART on FHIR, no `$export`, no terminology service.
  Those are what HAPI and Aidbox mostly are, and swapping the store doesn't
  provide them.
