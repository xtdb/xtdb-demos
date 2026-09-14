#!/usr/bin/env python
"""Walk one correction end to end and print the numbers, for the blog post.

    uv run python bin/correction-demo.py

Finds the pending chargeback whose confirmation moves a real score the most, scores the
affected transaction, confirms the chargeback, scores it again, then rewinds system time
to just before the confirmation and scores it a third time. Same model throughout; only
what the database knows has changed.

This WRITES to the demo node — confirming is a real transaction, and system-time only
moves forward, so a second run picks a different subject or finds none. For a clean slate, deliberately reset this demo's volumes
(see README.md), then run `./bin/seed.sh` against the empty database.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402


def main() -> int:
    cases = api.impactful(limit=1)["cases"]
    if not cases:
        print("no pending corrections with a measurable impact.\n"
              "the sim needs to have run far enough to have chargebacks in flight; "
              "try ./bin/seed.sh and let the sim tick.", file=sys.stderr)
        return 1

    c = cases[0]
    subj = api.subject_impact(
        account_id=c["account_id"], txn_id=c["txn_id"], ts=c["valid_time"],
        amount=c["amount"], country=c["country"])["subject"]

    res = api.audit_confirm(api.ConfirmReq(
        account_id=subj["account_id"], later_ts=subj["valid_time"],
        amount=subj["amount"], country=subj["country"],
        model_version=subj["model_version"]))

    pct = lambda p: f"{p * 100:.1f}%"
    print(f"account          {subj['account_id']}")
    print(f"transaction      {subj['txn_id']}  {subj['amount']:.2f} {subj['country']}")
    print(f"model            {res['model_version']}")
    print(f"chargebacks      {res['n_confirmed']} confirmed, {res['n_prior']} of them "
          f"earlier than this transaction")
    print()
    print(f"  before         {pct(res['before'])}   prior_confirmed_fraud = {res['pcf_before']:.0f}")
    print(f"  after          {pct(res['after'])}   prior_confirmed_fraud = {res['pcf_after']:.0f}")
    print(f"  reproduced     {pct(res['reproduced'])}   SYSTEM_TIME AS OF {res['system_time']}")
    print()
    print("--- paste into the post ---")
    print(f"scored {pct(res['before'])} on arrival; after {res['n_prior']} earlier "
          f"chargeback{'s' if res['n_prior'] != 1 else ''} settled, the same model scored "
          f"the same transaction {pct(res['after'])}; rewound to just before the "
          f"confirmation it scores {pct(res['reproduced'])} again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
