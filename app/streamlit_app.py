"""
streamlit_app.py
====================================
Streamlit user interface for Copper. It lets users define a chiller or a unitary DX unit,
generate a matching set of performance curves, inspect them, download them as IDF, CSV or JSON,
and explore the curve libraries shipped with Copper.

Run from the repository root with:

    streamlit run app/streamlit_app.py
"""

import os
import sys
from datetime import datetime

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

# Allow running the app from a source checkout without installing the package
APP_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (APP_DIR, os.path.dirname(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import copper as cp
import ui_backend as ub

COPPER = "#B87333"
GREY = "#6B7280"

CSS = """
<style>
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding-top: 4.5rem; max-width: 1500px;}  /* clears Streamlit's fixed toolbar */
  .cu-header {
    background: linear-gradient(100deg, #1F2937 0%, #374151 60%, #B87333 140%);
    border-radius: 12px; padding: 18px 26px; margin-bottom: 14px; color: #fff;
    display: flex; align-items: center; justify-content: space-between;
  }
  .cu-brand {display: flex; align-items: center; gap: 16px;}
  .cu-logo {width: 40px; height: 40px; flex: none; border-radius: 10px; background: #B87333;
    box-shadow: inset 0 0 0 5px rgba(255,255,255,.25);}
  .cu-title {font-size: 1.7rem; font-weight: 700; letter-spacing: 1px; line-height: 1.15; color: #fff;}
  .cu-sub {margin-top: 3px; color: #D1D5DB; font-size: .9rem; line-height: 1.3;}
  .cu-badge {background: rgba(255,255,255,.14); border-radius: 999px; padding: 4px 12px;
    font-size: .78rem; color: #fff; white-space: nowrap;}
  .cu-step {font-weight: 700; font-size: 1.02rem; margin-bottom: 4px;}
  .cu-step span {background: #B87333; color: #fff; border-radius: 50%; width: 24px; height: 24px;
    display: inline-flex; align-items: center; justify-content: center; font-size: .8rem; margin-right: 8px;}
  .cu-kpi {border: 1px solid #E5E7EB; border-left: 5px solid #9CA3AF; border-radius: 10px;
    padding: 12px 16px; background: #FAFAFA; height: 100%;}
  .cu-kpi.ok {border-left-color: #16A34A;} .cu-kpi.warn {border-left-color: #D97706;}
  .cu-kpi-l {font-size: .75rem; text-transform: uppercase; letter-spacing: .6px; color: #6B7280;}
  .cu-kpi-v {font-size: 1.75rem; font-weight: 700; color: #111827; line-height: 1.25;}
  .cu-kpi-s {font-size: .8rem; color: #6B7280;}
  .cu-empty {border: 2px dashed #D1D5DB; border-radius: 14px; padding: 46px 30px; text-align: center; color: #4B5563;}
  .cu-empty h3 {color: #1F2937; margin-bottom: 6px;}
</style>
"""

DEFAULTS = {
    "c": {
        "ref_cap": 500.0, "ref_cap_unit": "ton", "full_eff": 0.56, "full_eff_unit": "kW/ton",
        "part_eff": 0.52, "part_eff_unit": "kW/ton", "compressor_type": "centrifugal",
        "condenser_type": "water", "compressor_speed": "variable", "model": "ect_lwt",
        "min_unloading": 0.1,
    },
    "d": {
        "cap_kind": "Net", "ref_cap": 8.0, "ref_cap_unit": "ton", "full_eff": 11.55,
        "full_eff_unit": "eer", "part_eff": 14.8, "part_eff_unit": "eer",
        "compressor_type": "scroll", "compressor_speed": "constant",
    },
}
PREFIX = {"Chiller": "c", "UnitaryDirectExpansion": "d"}
SHORT = {"Chiller": "Chiller", "UnitaryDirectExpansion": "DX"}


def init_state():
    for prefix, values in DEFAULTS.items():
        for field, val in values.items():
            st.session_state.setdefault(f"{prefix}_{field}", val)
    st.session_state.setdefault("eqp_type", "Chiller")
    st.session_state.setdefault("name", "copper_curves")
    st.session_state.setdefault("runs", [])


def step_title(num, text):
    st.markdown(f'<div class="cu-step"><span>{num}</span>{text}</div>', unsafe_allow_html=True)


def equipment_card():
    """Equipment definition form. Returns (equipment type, parameters)."""
    with st.container(border=True):
        step_title(1, "Define the equipment")
        eqp_type = (
            st.segmented_control(
                "Equipment type",
                ["Chiller", "UnitaryDirectExpansion"],
                format_func=lambda t: {"Chiller": "Chiller", "UnitaryDirectExpansion": "Unitary DX"}[t],
                key="eqp_type",
                width="stretch",
            )
            or "Chiller"
        )
        k = PREFIX[eqp_type]
        p = {"name": st.text_input("Curve set name", key="name")}

        if eqp_type == "UnitaryDirectExpansion":
            p["cap_kind"] = st.radio(
                "Capacity basis", ["Net", "Gross"], horizontal=True, key=f"{k}_cap_kind",
                help="Gross capacity includes the indoor fan heat, net capacity does not.",
            )
        c1, c2 = st.columns([3, 2])
        p["ref_cap"] = c1.number_input(
            "Reference capacity", min_value=0.0, step=10.0 if eqp_type == "Chiller" else 1.0,
            key=f"{k}_ref_cap",
        )
        cap_units = ["ton", "kW", "W"] + (["kbtu/h"] if eqp_type != "Chiller" else [])
        p["ref_cap_unit"] = c2.selectbox("Unit", cap_units, key=f"{k}_ref_cap_unit")

        eff_units = ["kW/ton", "cop", "eer"] if eqp_type == "Chiller" else ["eer", "cop", "kW/ton"]
        part_label = "Part load efficiency (IPLV)" if eqp_type == "Chiller" else "Part load efficiency (IEER)"
        for field, label in [("full_eff", "Full load efficiency"), ("part_eff", part_label)]:
            c1, c2 = st.columns([3, 2])
            p[field] = c1.number_input(label, min_value=0.0, step=0.01, format="%.3f", key=f"{k}_{field}")
            p[f"{field}_unit"] = c2.selectbox("Unit", eff_units, key=f"{k}_{field}_unit")

        c1, c2 = st.columns(2)
        if eqp_type == "Chiller":
            p["compressor_type"] = c1.selectbox(
                "Compressor", ["centrifugal", "screw", "scroll", "reciprocating",
                               "positive_displacement", "scroll/screw", "any"],
                key="c_compressor_type",
            )
            p["compressor_speed"] = c2.selectbox(
                "Compressor speed", ["variable", "constant", "any"], key="c_compressor_speed"
            )
            p["condenser_type"] = c1.selectbox("Condenser", ["water", "air"], key="c_condenser_type")
            with st.expander("More chiller options"):
                p["model"] = st.selectbox(
                    "Model", ["ect_lwt", "lct_lwt"], key="c_model",
                    help="ect_lwt is the DOE-2 model, lct_lwt the reformulated EIR model.",
                )
                p["min_unloading"] = st.number_input(
                    "Minimum unloading ratio", 0.0, 1.0, step=0.05, key="c_min_unloading"
                )
        else:
            p["compressor_type"] = c1.selectbox("Compressor", ["scroll", "any"], key="d_compressor_type")
            p["compressor_speed"] = c2.selectbox(
                "Compressor speed", ["constant", "variable", "any"], key="d_compressor_speed"
            )
            c1.selectbox("Condenser", ["air"], disabled=True, key="d_condenser_type_fixed")
    return eqp_type, p


def generator_card(eqp_type):
    """Generator settings. Returns them as a dict."""
    k = PREFIX[eqp_type]
    ga = {}
    with st.container(border=True):
        step_title(2, "Choose how to generate")
        methods = ["nearest_neighbor", "weighted_average", "best_match"]
        if eqp_type != "Chiller":
            methods.insert(0, methods.pop())  # best_match is the closest for DX
        ga["method"] = st.selectbox(
            "Method", methods, key=f"method_{k}",
            help="best_match: closest library curves. nearest_neighbor: weighted average of the "
                 "N closest. weighted_average: weighted average of the whole library.",
        )
        options = ub.CURVE_VARS[eqp_type]
        selected = st.multiselect(
            "Curves to optimize", options,
            default=options if eqp_type == "Chiller" else ["eir-f-t"],
            key=f"vars_{k}",
            help="The genetic algorithm only modifies the selected curves. The others keep "
                 "the values of the base curve set found in the library.",
        )
        ga["vars"] = "" if set(selected) == set(options) else selected
        ga["selected"] = selected
        if eqp_type != "Chiller":
            st.caption(
                "Optimizing every DX curve currently fails for single-stage units in the "
                "Copper library, so only eir-f-t is selected by default."
            )
        ga["tol"] = st.number_input(
            "Tolerance", 0.0005, 0.1, 0.005, 0.0005, format="%.4f", key="tol",
            help="Maximum relative error accepted on both rated efficiencies.",
        )
        with st.expander("Advanced genetic algorithm settings"):
            c1, c2 = st.columns(2)
            ga["max_gen"] = c1.number_input("Max generations", 10, 2000, 300, 10, key="max_gen")
            ga["pop_size"] = c2.number_input("Population size", 10, 1000, 100, 10, key="pop_size")
            ga["num_neighbors"] = c1.number_input("Nearest neighbors", 1, 50, 10, key="num_neighbors")
            fixed = c2.checkbox("Fixed random seed", value=True, key="fixed_seed")
            seed = c2.number_input("Seed", 0, 10**6, 1, disabled=not fixed, key="seed")
            ga["seed"] = int(seed) if fixed else None
    return ga


def kpi(label, value, sub, tone=""):
    return (
        f'<div class="cu-kpi {tone}"><div class="cu-kpi-l">{label}</div>'
        f'<div class="cu-kpi-v">{value}</div><div class="cu-kpi-s">{sub}</div></div>'
    )


def style_axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(alpha=0.25)
    ax.tick_params(labelsize=8)


def plot_sets(sets, out_vars, norm, color, alpha, max_cols=3):
    """Plot the given SetofCurves objects, one subplot per output variable."""
    ncols = min(max_cols, len(out_vars))
    nrows = -(-len(out_vars) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.4 * ncols, 3.4 * nrows), squeeze=False)
    flat = list(axes.flat)
    for i, var in enumerate(out_vars):
        for s in sets:
            s.plot(out_var=[var], axes=[flat[i]], norm=norm, color=color, alpha=alpha)
        flat[i].set_title(var, fontsize=10, fontweight="bold")
        flat[i].set_xlabel(ub.X_LABELS.get(var.split("-")[-1], ""), fontsize=8)
        flat[i].set_ylabel("Normalized output" if norm else "Output", fontsize=8)
        style_axes(flat[i])
    for ax in flat[len(out_vars):]:
        ax.axis("off")
    fig.tight_layout()
    return fig


def results_panel(result):
    p, ga, achieved = result["params"], result["ga"], result["achieved"]
    met = ub.targets_met(result)
    tone = "ok" if met else "warn"

    cols = st.columns(4)
    for col, (label, key) in zip(cols[:2], [("Full load", "full"), ("Part load", "part")]):
        target, got = p[f"{key}_eff"], achieved[key]
        unit = p[f"{key}_eff_unit"]
        dev = (got - target) / target
        col.markdown(
            kpi(f"{label} ({unit})", f"{got:.3f}", f"target {target:.3f} · {dev:+.2%}", tone),
            unsafe_allow_html=True,
        )
    cols[2].markdown(
        kpi("Status", "Targets met" if met else "Not met", f"tolerance ±{ga['tol']:.2%}", tone),
        unsafe_allow_html=True,
    )
    cols[3].markdown(
        kpi("Curve set", f"{len(result['curves'])} curves", f"{ga['method']} · {result['elapsed']:.1f} s"),
        unsafe_allow_html=True,
    )
    if not met:
        st.warning(
            "The generated curves do not reproduce the target efficiencies within the tolerance, "
            "so they should not be used as is. Try another method, optimize more curves, or "
            "increase the tolerance or the maximum number of generations."
        )

    t_curves, t_coef, t_export, t_log = st.tabs(["Curves", "Coefficients", "Export", "Log"])
    soc = ub.make_set(result)
    with t_curves:
        c1, c2 = st.columns([3, 2])
        norm = c1.toggle("Normalize to rated conditions", value=False)
        names = [c.out_var for c in soc.curves]
        shown = c2.multiselect("Curves shown", names, default=names, label_visibility="collapsed")
        if shown:
            fig = plot_sets([soc], shown, norm, COPPER, 1.0)
            st.pyplot(fig)
            plt.close(fig)
        for var in shown:
            st.caption(f"**{var}** — {ub.CURVE_DESCRIPTIONS.get(var, '')}")
    with t_coef:
        st.dataframe(ub.coefficients_table(result["curves"]), hide_index=True, width="stretch")
    with t_export:
        name = ub.sanitize_name(result["name"])
        formats = [
            ("idf", "EnergyPlus IDF", "Curve objects ready to paste into an IDF.", "text/plain"),
            ("csv", "CSV", "One row per curve with its coefficients.", "text/csv"),
            ("json", "JSON", "Full curve data, usable as a Copper library entry.", "application/json"),
        ]
        for col, (fmt, title, desc, mime) in zip(st.columns(3), formats):
            with col.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(desc)
                st.download_button(
                    f"Download .{fmt}", ub.export_text(result, fmt), file_name=f"{name}.{fmt}",
                    mime=mime, width="stretch", key=f"dl_{fmt}_{result['id']}",
                )
        st.markdown("**IDF preview**")
        st.code(ub.export_text(result, "idf"), language="text", height=280)
    with t_log:
        st.code("\n".join(result["logs"]) or "No log output.", language="text", height=320)


def empty_state():
    st.markdown(
        """<div class="cu-empty"><h3>No curves generated yet</h3>
        Describe your equipment on the left, pick a generation method, and press
        <b>Generate curves</b>.<br>Results, plots, coefficients and downloads will appear here.
        </div>""",
        unsafe_allow_html=True,
    )


def generator_tab():
    left, right = st.columns([5, 8], gap="large")
    with left:
        eqp_type, p = equipment_card()
        ga = generator_card(eqp_type)
        problems = ub.validate_inputs(p)
        if not ga["selected"]:
            problems.append("Select at least one curve to optimize.")
        for msg in problems:
            st.error(msg, icon="⚠️")
        clicked = st.button(
            "Generate curves", type="primary", width="stretch", disabled=bool(problems)
        )

    with right:
        error = None
        if clicked:
            slow = " DX equipment can take a minute." if eqp_type != "Chiller" else ""
            with st.status(f"Running the genetic algorithm…{slow}", expanded=False) as status:
                try:
                    result = ub.run_generation(eqp_type, p, ga)
                    result["id"] = len(st.session_state["runs"]) + 1
                    result["time"] = datetime.now().strftime("%H:%M:%S")
                    st.session_state["runs"].append(result)
                    st.session_state["run_pick"] = len(st.session_state["runs"]) - 1
                    status.update(label=f"Done in {result['elapsed']:.1f} s", state="complete")
                except Exception as e:  # surface library errors in the page
                    error = f"{type(e).__name__}: {e}"
                    status.update(label="Generation failed", state="error")
        if error:
            st.error(f"Curve generation failed. {error}")

        runs = st.session_state["runs"]
        if not runs:
            if not error:
                empty_state()
            return

        pick_col, clear_col = st.columns([5, 1], vertical_alignment="bottom")
        idx = pick_col.selectbox(
            "Run history",
            range(len(runs) - 1, -1, -1),
            format_func=lambda i: (
                f"#{runs[i]['id']} · {runs[i]['name']} · {SHORT[runs[i]['eqp_type']]} · "
                f"{runs[i]['time']} · {'✓ met' if ub.targets_met(runs[i]) else '⚠ not met'}"
            ),
            key="run_pick",
        )
        if clear_col.button("Clear", width="stretch"):
            st.session_state["runs"] = []
            st.session_state.pop("run_pick", None)
            st.rerun()
        results_panel(runs[idx])


@st.cache_resource(show_spinner="Loading library…")
def load_library(eqp_type):
    return cp.Library(path=ub.library_path(eqp_type))


@st.cache_resource(show_spinner="Reading curves from the library…")
def cached_library_sets(eqp_type, selections):
    return ub.library_sets(load_library(eqp_type), eqp_type, dict(selections))


def library_tab():
    st.markdown(
        "Browse the performance curves that Copper uses as seeds. Every line is one curve set from the library."
    )
    c1, c2 = st.columns([1, 3])
    eqp_type = c1.segmented_control(
        "Library", ["Chiller", "UnitaryDirectExpansion"],
        format_func=lambda t: {"Chiller": "Chiller", "UnitaryDirectExpansion": "Unitary DX"}[t],
        default="Chiller", key="lib_type",
    ) or "Chiller"
    lib = load_library(eqp_type)
    options = ub.library_filter_options(lib)
    selections = {}
    with c2:
        fcols = st.columns(len(options) or 1)
        for col, (field, values) in zip(fcols, options.items()):
            selections[field] = col.multiselect(
                field.replace("_", " ").capitalize(), values, key=f"lib_{eqp_type}_{field}",
                placeholder="All",
            )
    c1, c2, c3 = st.columns([3, 2, 2], vertical_alignment="center")
    out_vars = c1.multiselect(
        "Curves", ub.CURVE_VARS[eqp_type],
        default=["eir-f-t", "cap-f-t"] if eqp_type == "Chiller" else ["eir-f-t", "cap-f-t", "eir-f-ff"],
        key=f"lib_vars_{eqp_type}",
    )
    alpha = c2.slider("Line opacity", 0.05, 1.0, 0.25, 0.05)
    norm = c3.toggle("Normalize", value=False, key="lib_norm")

    sets = cached_library_sets(eqp_type, tuple(sorted((f, tuple(v)) for f, v in selections.items())))
    st.caption(f"{len(sets)} curve sets match the filters.")
    if not sets:
        st.info("No curve sets match these filters.")
        return
    if out_vars:
        fig = plot_sets(sets, out_vars, norm, COPPER, alpha)
        st.pyplot(fig)
        plt.close(fig)
    with st.expander("Curve sets in the selection"):
        rows = [
            {"name": s.name, **{f: getattr(s.eqp, f, None) for f in ub.LIBRARY_FILTER_FIELDS}}
            for s in sets
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def help_tab():
    st.markdown(
        """
### How Copper works
Copper takes a few design characteristics of an HVAC unit and returns simulation-ready performance
curves. It starts from curves in its library, then modifies them with a genetic algorithm until the
unit reproduces your **full load** and **part load** efficiency.

### Using this tool
1. **Define the equipment** in step 1: type, capacity, and full and part load efficiency.
2. **Choose how to generate** in step 2: the method, which curves the algorithm may change, and the tolerance.
3. Press **Generate curves**. Each run is kept in the run history so you can compare them.
4. Check the *Status* tile, inspect the plots and coefficients, then download the curves from the *Export* tab.

### Generation methods
| Method | What it does |
|---|---|
| `best_match` | Uses the single closest curve set in the library as the starting point |
| `nearest_neighbor` | Weighted average of the N closest curve sets |
| `weighted_average` | Weighted average of every curve set in the library |

### Efficiency metrics
| | Chiller | Unitary DX |
|---|---|---|
| Full load | Rated efficiency at AHRI 550/590 conditions | Rated efficiency at AHRI 340/360 conditions |
| Part load | IPLV | IEER |

### Known limitations
- The generator returns its best attempt even when it misses the targets. Always check the *Status* tile.
- Optimizing every curve of a single-stage DX unit currently fails inside the Copper library; the app
  defaults to `eir-f-t` only for DX.
- DX generation can take about a minute.
"""
    )


def main():
    st.set_page_config(page_title="Copper", page_icon="🔶", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()
    st.markdown(
        '<div class="cu-header"><div class="cu-brand"><div class="cu-logo"></div><div>'
        '<div class="cu-title">COPPER</div>'
        '<div class="cu-sub">Performance curve generator for building energy simulation</div>'
        '</div></div><span class="cu-badge">Chillers · Unitary DX</span></div>',
        unsafe_allow_html=True,
    )
    t_gen, t_lib, t_help = st.tabs(["⚙️  Curve Generator", "📚  Library Explorer", "❓  Help"])
    with t_gen:
        generator_tab()
    with t_lib:
        library_tab()
    with t_help:
        help_tab()


main()
