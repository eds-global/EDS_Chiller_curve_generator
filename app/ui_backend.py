"""
ui_backend.py
====================================
Non-visual helpers for the Copper Streamlit app: building equipment, running the generator,
exporting curves and querying the curve libraries. Kept free of Streamlit calls so they can be tested directly.
"""

import json
import logging
import os
import re
import tempfile
import time

import pandas as pd

import copper as cp
from copper.curves import SetofCurves

DATA_DIR = os.path.join(os.path.dirname(cp.__file__), "data")
LIBRARY_FILES = {
    "Chiller": "chiller_curves.json",
    "UnitaryDirectExpansion": "unitarydirectexpansion_curves.json",
}
CURVE_VARS = {
    "Chiller": ["eir-f-t", "cap-f-t", "eir-f-plr"],
    "UnitaryDirectExpansion": ["eir-f-t", "cap-f-t", "eir-f-ff", "cap-f-ff", "plf-f-plr"],
}
# Library filters offered by the explorer and the values Copper can build equipment for
LIBRARY_FILTER_FIELDS = ["compressor_type", "compressor_speed", "condenser_type", "model"]
SUPPORTED_VALUES = {"condenser_type": {"air", "water"}}
CURVE_DESCRIPTIONS = {
    "eir-f-t": "Energy input ratio as a function of temperature",
    "cap-f-t": "Capacity as a function of temperature",
    "eir-f-plr": "Energy input ratio as a function of part load ratio",
    "eir-f-ff": "Energy input ratio as a function of flow fraction",
    "cap-f-ff": "Capacity as a function of flow fraction",
    "plf-f-plr": "Part load fraction as a function of part load ratio",
}
X_LABELS = {"t": "Temperature (°C)", "plr": "Part load ratio", "ff": "Flow fraction"}


class ListHandler(logging.Handler):
    """Collects log records so they can be displayed in the UI."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


def sanitize_name(name):
    """Make a name safe for use as a file name and as an IDF object name."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name.strip()) or "copper_curves"


def validate_inputs(p):
    """Return a list of problems with the form values, empty when the inputs are usable."""
    problems = []
    for key, label in [
        ("ref_cap", "Reference capacity"),
        ("full_eff", "Full load efficiency"),
        ("part_eff", "Part load efficiency"),
    ]:
        if not p.get(key) or p[key] <= 0:
            problems.append(f"{label} must be greater than zero.")
    if not p.get("name", "").strip():
        problems.append("Give the curve set a name.")
    return problems


def build_equipment(eqp_type, p):
    """Create a Copper equipment object from the values entered in the form."""
    if eqp_type == "Chiller":
        return cp.Chiller(
            ref_cap=p["ref_cap"],
            ref_cap_unit=p["ref_cap_unit"],
            full_eff=p["full_eff"],
            full_eff_unit=p["full_eff_unit"],
            part_eff=p["part_eff"],
            part_eff_unit=p["part_eff_unit"],
            compressor_type=p["compressor_type"],
            condenser_type=p["condenser_type"],
            compressor_speed=p["compressor_speed"],
            model=p["model"],
            min_unloading=p["min_unloading"],
            set_of_curves=[],  # Chiller's default is a list shared between instances
        )
    cap_arg = "ref_gross_cap" if p["cap_kind"] == "Gross" else "ref_net_cap"
    return cp.UnitaryDirectExpansion(
        **{cap_arg: p["ref_cap"]},
        ref_cap_unit=p["ref_cap_unit"],
        full_eff=p["full_eff"],
        full_eff_unit=p["full_eff_unit"],
        part_eff=p["part_eff"],
        part_eff_unit=p["part_eff_unit"],
        compressor_type=p["compressor_type"],
        condenser_type="air",
        compressor_speed=p["compressor_speed"],
    )


def run_generation(eqp_type, p, ga):
    """Generate the curves. Returns a dict with the equipment, curves, rated efficiencies and log lines."""
    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    start = time.time()
    try:
        eqp = build_equipment(eqp_type, p)
        curves = eqp.generate_set_of_curves(
            method=ga["method"],
            tol=ga["tol"],
            max_gen=ga["max_gen"],
            pop_size=ga["pop_size"],
            vars=ga["vars"],
            num_nearest_neighbors=ga["num_neighbors"],
            random_seed=ga["seed"],
        )
    finally:
        root.removeHandler(handler)

    if not curves:
        raise RuntimeError(
            "The generator did not converge. Increase the tolerance or the maximum "
            "number of generations and try again."
        )

    return {
        "eqp": eqp,
        "eqp_type": eqp_type,
        "curves": curves,
        "logs": handler.records,
        "name": p["name"],
        "params": dict(p),
        "ga": dict(ga),
        "elapsed": time.time() - start,
        "achieved": {
            "full": eqp.calc_rated_eff("full", unit=p["full_eff_unit"]),
            "part": eqp.calc_rated_eff("part", unit=p["part_eff_unit"]),
        },
    }


def targets_met(result):
    """Check that the generated curves reproduce the target efficiencies within tolerance."""
    p, tol, achieved = result["params"], result["ga"]["tol"], result["achieved"]
    return (
        abs(achieved["full"] - p["full_eff"]) <= tol * p["full_eff"]
        and abs(achieved["part"] - p["part_eff"]) <= tol * p["part_eff"]
    )


def make_set(result):
    """Wrap the generated curves in a SetofCurves object."""
    soc = SetofCurves()
    soc.curves = result["curves"]
    soc.eqp = result["eqp"]
    soc.name = result["name"]
    return soc


def coefficients_table(curves):
    """Tabulate the coefficients of each curve."""
    rows = []
    for c in curves:
        row = {"curve": c.out_var, "type": c.type}
        for i in range(1, c.nb_coeffs() + 1):
            row[f"coeff{i}"] = getattr(c, f"coeff{i}")
        rows.append(row)
    return pd.DataFrame(rows)


def export_text(result, fmt):
    """Export the curves to a string in the requested format (idf, csv or json)."""
    name = sanitize_name(result["name"])
    if fmt == "json":
        # SetofCurves.export(fmt="json") removes the equipment from the curve objects,
        # so build the JSON here to keep the curves usable by the rest of the page.
        data = {
            name: [
                {k: v for k, v in c.__dict__.items() if k != "eqp"}
                for c in result["curves"]
            ]
        }
        return json.dumps(data, indent=4)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        make_set(result).export(path=tmp, fmt=fmt, name=name)
        with open(os.path.join(tmp, f"{name}.{fmt}"), "r", encoding="utf-8") as f:
            return f.read()


def library_path(eqp_type):
    return os.path.join(DATA_DIR, LIBRARY_FILES[eqp_type])


def library_filter_options(lib):
    """Unique, hashable values of the filterable fields found in a library."""
    options = {}
    for entry in lib.data.values():
        for field in LIBRARY_FILTER_FIELDS:
            val = entry.get(field)
            if isinstance(val, str):
                options.setdefault(field, set()).add(val)
    for field, allowed in SUPPORTED_VALUES.items():
        if field in options:
            options[field] &= allowed
    return {field: sorted(vals) for field, vals in options.items()}


def library_sets(lib, eqp_type, selections):
    """Sets of curves in a library matching the selected filter values.

    :param dict selections: Mapping of field to the list of accepted values, an empty list accepts anything
    """
    options = library_filter_options(lib)
    filters = [("eqp_type", eqp_type), ("sim_engine", "energyplus")]
    sets = []
    # Library.find_set_of_curves_from_lib takes a single value per field, so query value by value
    fields = [f for f in LIBRARY_FILTER_FIELDS if f in options]
    combos = [[]]
    for field in fields:
        values = selections.get(field) or options[field]
        combos = [c + [(field, v)] for c in combos for v in values]
    for combo in combos:
        try:
            sets.extend(lib.find_set_of_curves_from_lib(filters=filters + combo))
        except Exception as e:  # some library entries cannot be instantiated
            logging.warning(f"Skipped library filter {combo}: {e}")
    return sets
