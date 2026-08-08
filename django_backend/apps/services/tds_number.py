"""
apps/services/tds_number.py — Atomic TDS number generator.

Format: 4-digit zero-padded integer, e.g. '0001', '0042', '1000'.
No year prefix — global counter across all years.

Implementation
--------------
Uses the existing ``tds_sequence`` table via the TDSSequence Django ORM model.
A sentinel row with ``year = 0`` holds the global counter.
``select_for_update()`` (SELECT … FOR UPDATE) inside ``transaction.atomic()``
prevents race conditions when two requests arrive simultaneously.

Usage
-----
    from apps.services.tds_number import next_tds_number
    tds_number = next_tds_number()   # e.g. "0001"
"""

from django.db import transaction

from apps.core.models import TDSSequence


def next_tds_number() -> str:
    """
    Atomically increment the global TDS counter and return a 4-digit
    zero-padded string (e.g. '0001', '0042', '1000').

    Uses ``year = 0`` as the sentinel row for the global counter.

    Must be called inside a transaction.atomic() block in the view
    so the increment and the new TDS row stay in the same transaction.
    """
    with transaction.atomic():
        seq = (
            TDSSequence.objects
            .select_for_update()
            .filter(year=0)
            .first()
        )

        if seq is None:
            seq = TDSSequence(year=0, last_number=1)
            seq.save()
        else:
            seq.last_number += 1
            seq.save()

        return f"{seq.last_number:04d}"
