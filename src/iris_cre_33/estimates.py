
import math


def estimate_pv_kwp(
    footprint_area_sqm,
    usable_fraction=0.60,
    sqm_per_kwp=5.0,
):
    """Estimate PV capacity from a building footprint area."""

    if (
        not math.isfinite(footprint_area_sqm)
        or footprint_area_sqm <= 0
    ):
        raise ValueError("Footprint area must be positive and finite.")

    if (
        not math.isfinite(usable_fraction)
        or not 0 < usable_fraction <= 1
    ):
        raise ValueError("Usable fraction must be between 0 and 1.")

    if not math.isfinite(sqm_per_kwp) or sqm_per_kwp <= 0:
        raise ValueError("Square metres per kWp must be positive.")

    usable_area_sqm = footprint_area_sqm * usable_fraction
    pv_kwp = usable_area_sqm / sqm_per_kwp

    return {
        "usable_roof_area_sqm": usable_area_sqm,
        "estimated_pv_kwp": pv_kwp,
        "usable_fraction": usable_fraction,
        "sqm_per_kwp": sqm_per_kwp,
    }
