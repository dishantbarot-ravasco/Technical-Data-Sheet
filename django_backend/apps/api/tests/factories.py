"""
apps/api/tests/factories.py — Minimal fixture builders shared by integration tests.

Keeps each test focused on the behavior under test instead of re-deriving the
TDSInput FK graph (Purpose -> BeltType -> Brand -> Standard -> CoverGrade ->
FabricType -> BeltRating) every time.
"""
import itertools

import bcrypt

from apps.core.models import (
    TDSUser, Purpose, BeltType, IndusBrand, Standard, CoverGrade,
    FabricType, BeltRating, BeltRatingValue, TDSParameter, ReelType, PackingType,
)

PARAM_INTERPLY_SKIM = 5

# Purpose/BeltType/IndusBrand/Standard/TDSParameter are static reference tables
# seeded once outside Django (their PK columns have no DB-side default/identity —
# verified against the live schema), so every INSERT must supply an explicit PK.
# This counter just needs to be unique per test process, not stable across runs —
# each Django TestCase wraps a test in a transaction that's rolled back after it.
_next_legacy_pk = itertools.count(900_000)


def make_user(email='creator@ravasco.com', password='Str0ngPassw0rd!', role='tds_creator', **extra):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return TDSUser.objects.create(
        email=email, password_hash=hashed, role=role, is_active=True, **extra
    )


def make_tds_lookup_set():
    """
    Build the minimal FK graph create_tds() needs: a Purpose, BeltType, Brand,
    Standard, CoverGrade (on that Standard), FabricType, and a BeltRating
    ('EP 315/3' — parses cleanly via parse_belt_rating) with its interply-skim
    EAV row so create_tds's server-computed thickness path doesn't crash.

    Returns a dict of the created rows, plus a `payload` dict pre-filled with
    everything create_tds requires so a test only has to override what it
    actually cares about.
    """
    # Suffix every unique=True text field with a counter value so calling this
    # more than once in a single test (e.g. to build two independent FK graphs)
    # never collides on a unique constraint.
    tag = next(_next_legacy_pk)

    purpose = Purpose.objects.create(purpose_id=next(_next_legacy_pk), purpose_type='Domestic')
    belt_type = BeltType.objects.create(belt_id=next(_next_legacy_pk), belt_type='Flat Open-End')
    brand = IndusBrand.objects.create(brand_id=next(_next_legacy_pk), brand_name=f'INDUS SUPER BRUTE {tag}')
    standard = Standard.objects.create(standard_id=next(_next_legacy_pk), standard_name=f'IS 1891-{tag}', brand=brand)
    cover_grade = CoverGrade.objects.create(
        standard=standard, grade_code='M24', specific_gravity=1.15,
    )
    fabric_type = FabricType.objects.create(fabric_code=f'EP-{tag}')
    belt_rating = BeltRating.objects.create(fabric_type=fabric_type, rating_name='EP 315/3')

    skim_param, _ = TDSParameter.objects.get_or_create(
        parameter_id=PARAM_INTERPLY_SKIM,
        defaults=dict(parameter_group='Construction', parameter_name='Interply Skim',
                      display_order=1),
    )
    BeltRatingValue.objects.create(
        belt_rating=belt_rating, parameter=skim_param, indus_value='0.5',
    )

    reel_type = ReelType.objects.create(
        reel_name=f'Circular-{tag}', formula_key='circular', num_rolls_base=1,
        core_diameter_m='0.3', max_roll_diameter_m='2.50',
    )
    packing_type = PackingType.objects.create(packing_name=f'Standard-{tag}', is_available=True)

    payload = {
        'standard_id': standard.pk,
        'belt_type_id': belt_type.pk,
        'brand_id': brand.pk,
        'purpose_id': purpose.pk,
        'cover_grade_id': cover_grade.pk,
        'belt_rating_id': belt_rating.pk,
        'fabric_type_id': fabric_type.pk,
        'belt_length_m': 100,
        'belt_width_mm': 1000,
        'num_plies': 3,
        'top_cover_mm': 3,
        'bottom_cover_mm': 1.5,
        'carcass_from_rating': 4.5,
        'carcass_thickness_mm': 4.5,
        'edge_construction': 'Moulded Edge',
        'construction_type': 'Open-End',
    }

    return {
        'purpose': purpose,
        'belt_type': belt_type,
        'brand': brand,
        'standard': standard,
        'cover_grade': cover_grade,
        'fabric_type': fabric_type,
        'belt_rating': belt_rating,
        'reel_type': reel_type,
        'packing_type': packing_type,
        'payload': payload,
    }
