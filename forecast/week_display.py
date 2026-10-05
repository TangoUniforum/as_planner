"""Show weeks as "W05 '27" everywhere the APP draws one -- display only.

The operator reads weeks week-first (2026-10-05: "W05 '27"), the workbook
keeps its ISO labels ("the export file is okay"), and the ISO label stays the
key everything else runs on: limits.yaml, manual events, caches, sorting.

So instead of editing ~150 call sites -- and missing the engine messages the
app prints verbatim -- this wraps the Streamlit calls that DRAW text, and
rewrites ISO week labels only in what is drawn:

  * text elements (markdown, caption, info/warning/..., headers, write) and
    the LABELS / help of widgets, buttons, expanders and tabs;
  * choice widgets (selectbox, multiselect, radio, select_slider, ...) through
    their format_func: the VALUE a widget returns stays ISO;
  * st.dataframe / st.table: a COPY of the data has its week cells rewritten,
    and week-named columns get a display label through column_config -- the
    column itself is never renamed, so column_config keys, column_order and
    click selections keep matching;
  * st.plotly_chart: a COPY of the figure has its week tick labels rewritten.

Deliberately NOT touched: st.data_editor (an editor returns what it shows, so
a rewritten cell would be saved back -- the Limits grid relabels its week
columns explicitly instead), st.code / st.json, text-input VALUES, and every
download (file names and bytes).

install() is idempotent: Streamlit re-executes app.py on every rerun, so a
second call must not wrap the wrappers.
"""
from __future__ import annotations

import functools

from .time_grid import display_week, display_weeks_in_text

_MARK = "_as_week_display"

# first positional arg (or the named kwarg) is display text
_LABEL_FIRST = {
    "markdown": "body", "caption": "body", "text": "body", "info": "body",
    "warning": "body", "error": "body", "success": "body", "toast": "body",
    "title": "body", "header": "body", "subheader": "body",
    "button": "label", "download_button": "label", "link_button": "label",
    "form_submit_button": "label", "expander": "label", "popover": "label",
    "checkbox": "label", "toggle": "label", "number_input": "label",
    "text_input": "label", "text_area": "label", "slider": "label",
    "date_input": "label", "time_input": "label", "file_uploader": "label",
    "color_picker": "label",
}
_CHOICE = ("selectbox", "multiselect", "radio", "select_slider", "pills",
           "segmented_control")
# kwargs that are display text wherever they appear
_TEXT_KWARGS = ("help", "placeholder", "caption")


def _t(v):
    return display_weeks_in_text(v)


def _kw_text(kwargs):
    for k in _TEXT_KWARGS:
        if k in kwargs:
            kwargs[k] = _t(kwargs[k])


def _first(args, kwargs, name):
    if args:
        args = (_t(args[0]),) + tuple(args[1:])
    elif name in kwargs:
        kwargs[name] = _t(kwargs[name])
    return args


# Each transform takes the call's (args, kwargs) WITHOUT `self` and returns the
# (args, kwargs) to draw with. _install_one applies it to both the bound
# module functions (st.markdown) and the DeltaGenerator methods (col.markdown).

def _tx_label_first(name):
    def tx(args, kwargs):
        args = _first(args, kwargs, name)
        _kw_text(kwargs)
        return args, kwargs
    return tx


def _tx_write(args, kwargs):
    return tuple(_t(a) for a in args), kwargs


def _tx_metric(args, kwargs):
    args = tuple(_t(a) for a in args)
    for k in ("label", "value", "delta"):
        if k in kwargs:
            kwargs[k] = _t(kwargs[k])
    _kw_text(kwargs)
    return args, kwargs


def _tx_tabs(args, kwargs):
    if args and isinstance(args[0], (list, tuple)):
        args = ([_t(x) for x in args[0]],) + tuple(args[1:])
    elif isinstance(kwargs.get("tabs"), (list, tuple)):
        kwargs["tabs"] = [_t(x) for x in kwargs["tabs"]]
    return args, kwargs


def _tx_choice(args, kwargs):
    args = _first(args, kwargs, "label")
    _kw_text(kwargs)
    # format_func is keyword at every call site; a positional one (third arg
    # onward) is left exactly as given.
    if len(args) <= 2:
        base = kwargs.get("format_func") or str
        kwargs["format_func"] = lambda o, _b=base: _t(str(_b(o)))
    return args, kwargs


def _week_cols(columns):
    out = []
    for c in columns:
        if isinstance(c, str) and display_week(c) != c:
            out.append(c)
    return out


def _relabel_config(data_cols, kwargs):
    """column_config labels for week-named columns (merged, never replacing
    an operator-facing label the caller set)."""
    wk = _week_cols(data_cols)
    if not wk:
        return
    cfg = dict(kwargs.get("column_config") or {})
    for c in wk:
        cur = cfg.get(c)
        if cur is None:
            cfg[c] = {"label": display_week(c)}
        elif isinstance(cur, dict) and not cur.get("label"):
            cur = dict(cur)
            cur["label"] = display_week(c)
            cfg[c] = cur
    kwargs["column_config"] = cfg


def _convert_frame(df):
    """A copy with every week label in string cells and in the index shown
    week-first. Column names are left alone (see _relabel_config)."""
    import pandas as pd
    out = df.copy()
    for c in out.columns:
        col = out[c]
        if col.dtype == object:
            out[c] = col.map(lambda v: _t(v) if isinstance(v, str) else v)
    if out.index.dtype == object:
        out.index = pd.Index([_t(v) if isinstance(v, str) else v
                              for v in out.index], name=out.index.name)
    return out


def _tx_frame(is_dataframe, data_kw="data"):
    def tx(args, kwargs):
        try:
            import pandas as pd
            data = args[0] if args else kwargs.get(data_kw)
            new, cols = data, ()
            if isinstance(data, pd.DataFrame):
                new, cols = _convert_frame(data), list(data.columns)
            elif hasattr(data, "data") and hasattr(data, "hide"):   # Styler
                cols = list(data.data.columns)       # cells keep their styling
            elif (isinstance(data, list) and data
                  and all(isinstance(r, dict) for r in data)):
                new = [{k: (_t(v) if isinstance(v, str) else v)
                        for k, v in r.items()} for r in data]
                cols = list(data[0].keys())
            if is_dataframe:
                _relabel_config(cols, kwargs)
            elif isinstance(new, pd.DataFrame) and _week_cols(cols):
                # st.table has no column_config: rename on the drawn copy.
                new = new.rename(columns={c: display_week(c)
                                          for c in _week_cols(cols)})
            if new is not data:
                if args:
                    args = (new,) + tuple(args[1:])
                else:
                    kwargs[data_kw] = new
        except Exception:  # noqa: BLE001 -- display sugar must never break a page
            pass
        _kw_text(kwargs)
        return args, kwargs
    return tx


def _convert_fig(fig):
    import plotly.graph_objects as go
    f = go.Figure(fig)
    for tr in f.data:
        for attr in ("x", "y"):
            vals = getattr(tr, attr, None)
            if vals is None:
                continue
            try:
                seq = list(vals)
            except TypeError:
                continue
            if any(isinstance(v, str) and display_week(v) != v for v in seq):
                setattr(tr, attr, [display_week(v) if isinstance(v, str) else v
                                   for v in seq])
        name = getattr(tr, "name", None)
        if isinstance(name, str):
            tr.name = _t(name)
    try:
        if f.layout.title and f.layout.title.text:
            f.layout.title.text = _t(f.layout.title.text)
        for ax in ("xaxis", "yaxis"):
            a = getattr(f.layout, ax, None)
            if a is not None and a.categoryarray is not None:
                a.categoryarray = [display_week(v) if isinstance(v, str) else v
                                   for v in a.categoryarray]
    except Exception:  # noqa: BLE001
        pass
    return f


def _tx_plotly(args, kwargs):
    try:
        if args:
            args = (_convert_fig(args[0]),) + tuple(args[1:])
        elif "figure_or_data" in kwargs:
            kwargs["figure_or_data"] = _convert_fig(kwargs["figure_or_data"])
    except Exception:  # noqa: BLE001
        pass
    return args, kwargs


def _transform_for(name):
    if name in _LABEL_FIRST:
        return _tx_label_first(_LABEL_FIRST[name])
    if name in _CHOICE:
        return _tx_choice
    return {"write": _tx_write, "metric": _tx_metric, "tabs": _tx_tabs,
            "dataframe": _tx_frame(True), "table": _tx_frame(False),
            "plotly_chart": _tx_plotly}.get(name)


def _wrap(fn, tx, has_self):
    @functools.wraps(fn)
    def w(*args, **kwargs):
        if has_self:
            a, k = tx(tuple(args[1:]), dict(kwargs))
            return fn(args[0], *a, **k)
        a, k = tx(tuple(args), dict(kwargs))
        return fn(*a, **k)
    setattr(w, _MARK, True)
    return w


NAMES = tuple(_LABEL_FIRST) + _CHOICE + ("write", "metric", "tabs",
                                         "dataframe", "table", "plotly_chart")


def install(st_module) -> int:
    """Wrap the drawing calls of `st_module` and of DeltaGenerator (so column /
    sidebar / expander containers are covered too). Returns how many callables
    were wrapped this time (0 on a repeat call)."""
    from streamlit.delta_generator import DeltaGenerator
    n = 0
    for name in NAMES:
        tx = _transform_for(name)
        fn = getattr(DeltaGenerator, name, None)
        if fn is not None and callable(fn) and not getattr(fn, _MARK, False):
            setattr(DeltaGenerator, name, _wrap(fn, tx, has_self=True))
            n += 1
        mfn = getattr(st_module, name, None)
        if mfn is not None and not getattr(mfn, _MARK, False):
            setattr(st_module, name, _wrap(mfn, tx, has_self=False))
            n += 1
    return n
