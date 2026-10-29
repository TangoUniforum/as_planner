"""'Tank layouts' mode of the app: two ways to lay a finished plan out in the tanks with fewer moves.

  * Tank rota      - a fixed standard schedule in steps you choose (forecast/tank_rota.py), minutes, in the page.
  * Least-moves    - the same plan re-laid with the fewest moves (forecast/least_moves.py), ~2.5 h, in the background.

Both read a planned workbook and write a companion workbook. Neither changes the plan, the config or any report.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
JOBS = Path(tempfile.gettempdir()) / 'as_forecast_layout_jobs'
_DEFAULT_STEPS = [{'From week of life': 0, 'Tanks': 4}, {'From week of life': 21, 'Tanks': 6},
                  {'From week of life': 32, 'Tanks': 1}]


def _save_upload(up) -> Path:
    data = up.getvalue(); h = hashlib.sha1(data).hexdigest()[:12]
    d = JOBS / f'wb_{h}'; d.mkdir(parents=True, exist_ok=True)
    p = d / up.name
    if not p.exists():
        p.write_bytes(data)
    return p


def _endpoint(path: Path) -> dict:
    key = f'_tl_ep_{path}'
    if key not in st.session_state:
        from forecast import least_moves
        st.session_state[key] = least_moves.read_endpoint(str(path), str(ROOT / 'config'), str(ROOT / 'scenario'))
    return st.session_state[key]


def _steps_from_table(df: pd.DataFrame) -> list[dict]:
    rows = [r for r in df.to_dict('records')
            if pd.notna(r.get('From week of life')) and pd.notna(r.get('Tanks'))]
    steps = []
    for i, r in enumerate(rows):
        w, k = int(r['From week of life']), int(r['Tanks'])
        steps.append({'week': w, 'tanks': k} if i < 2 else {'week': w, 'add': k})
    return steps


def _rota_section(workers: int):
    from forecast import tank_rota
    st.subheader('Tank rota — a fixed schedule in steps')
    st.caption('Every batch handled the same way: it arrives into the entry tanks, moves once (whole batch) into '
               'grow-out, and can get extra grow tanks later. A one-second tank count says whether the steps can fit '
               'at all; the full check designs one batch, puts the batches on real tanks within the system caps, and '
               'judges three cycles with the same independent checker as least-moves. Steady state only — getting '
               'from today\'s plan into the rota is not worked out here.')
    up = st.file_uploader('Planned workbook (from Run forecast)', type=['xlsm', 'xlsx'], key='tl_rota_wb')
    if up is None:
        st.info('Upload a planned workbook. Its growth, mortality and 6N harvest timing are copied; '
                'it should have been made with the config that is live now.')
        return
    path = _save_upload(up)
    try:
        D = _endpoint(path)
    except Exception as e:                                   # a workbook that is not a planned run
        st.error(f'Could not read the plan from this workbook: {e}')
        return
    full = tank_rota.complete_batches(D)
    if not full:
        st.warning('No batch lives its whole life inside this plan — run the forecast further out.')
        return
    default = tank_rota.batch_profile(D)['batch']
    labels = {f['batch']: f"{f['batch']} — arrives {f['arrival']}, {f['weeks']} weeks, {f['fish']:,.0f} fish"
              for f in full}
    c1, c2 = st.columns(2)
    b = c1.selectbox('Copy the life of batch', list(labels), index=list(labels).index(default),
                     format_func=labels.get, key='tl_rota_batch',
                     help='Growth, mortality and the weeks of 6N staging come from this batch. Default: the latest '
                          'batch that is done a cadence before the plan ends (the last ones carry end-of-plan tails).')
    plan_fish = next(f['fish'] for f in full if f['batch'] == b)
    fish = c2.number_input('Fish per batch', min_value=10_000, max_value=1_000_000, step=10_000,
                           value=int(round(plan_fish, -3)), key=f'tl_rota_fish_{b}',
                           help='Scales the counts and the 6N staging; weights and timing stay as in the plan.')
    st.markdown('**Steps** — row 1: entry tanks at week 0. Row 2: the week the whole batch moves to grow-out and how '
                'many grow tanks. Rows 3+: a week and how many grow tanks to ADD (filled with pieces of at least '
                '7,000 fish from the tanks the batch already has).')
    df = st.data_editor(pd.DataFrame(_DEFAULT_STEPS), num_rows='dynamic', key='tl_rota_steps',
                        column_config={'From week of life': st.column_config.NumberColumn(min_value=0, step=1),
                                       'Tanks': st.column_config.NumberColumn(min_value=1, step=1)})
    steps = _steps_from_table(df)
    errs = tank_rota.validate_steps(steps, len(tank_rota._facility(D)['entry']))
    if errs:
        st.warning(' · '.join(errs))
        return
    prof = tank_rota.batch_profile(D, b, float(fish))
    q = tank_rota.quick_check(D, prof, steps)
    (st.success if q['ok'] else st.error)('**Tank count:** ' + ' '.join(q['lines']))
    st.caption(f"One batch every {prof['cadence']} weeks (the plan's arrivals); density cap {prof['cap']:g} kg/m3; "
               f"system caps x {1 + D['buffer']:.1f} as the engine.")
    if not q['ok']:
        return
    if st.button('▶ Run the full check (about 1–5 minutes)', key='tl_rota_go', type='primary'):
        with st.status('Designing the rota ...', expanded=True) as box:
            res = tank_rota.run(D, steps, b, float(fish), workers=workers, log=box.write)
            box.update(label='Done', state='complete')
        st.session_state['tl_rota_res'] = (str(path), b, float(fish), repr(steps), res)
    saved = st.session_state.get('tl_rota_res')
    if not saved or saved[:4] != (str(path), b, float(fish), repr(steps)):
        return
    res = saved[4]
    if res['stage'] != 'done':
        st.error(' '.join(res.get('lines', [])) or 'No rota found.')
        return
    ev = res['eval']
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('Rule check', 'clean' if res['ok'] else f"{sum(ev['check']['issues'].values())} breaks")
    m2.metric('Transfers per harvested fish', f"{ev['tpf']:.2f}")
    m3.metric('Move events per week (max)', ev['max_moves'])
    m4.metric('Busiest system', f"{ev['busiest_t']:,.0f} t", help=f"cap {ev['sys_cap_t']:.0f} t x buffer")
    if not res['ok']:
        st.error('Rule breaks: ' + ', '.join(f'{k} {v}' for k, v in ev['check']['issues'].items()))
    if ev['needs_buffer']:
        st.warning(f"Uses the system tolerance: a system goes over {ev['sys_cap_t']:.0f} t (within the buffer the "
                   f"engine allows).")
    out = path.with_name(f"{path.stem}_tank_rota_{b}_{int(fish)}.xlsx")
    tank_rota.write_workbook(res, str(out), path.name)
    st.download_button('⬇ Rota workbook (summary, batch recipe, tank calendar, transfers)', out.read_bytes(),
                       file_name=out.name, key='tl_rota_dl')


def _job_status(d: Path) -> tuple[str, str]:
    log = d / 'run.log'; out = next(iter(d.glob('*_least_moves.xlsx')), None)
    txt = log.read_text(errors='replace') if log.exists() else ''
    last = next((ln for ln in reversed(txt.splitlines()) if ln.strip()), '')
    if out is not None:
        return 'done', last
    if 'Traceback' in txt or 'Error' in last:
        return 'failed', last
    age = (time.time() - log.stat().st_mtime) / 60 if log.exists() else 0
    return ('running' if age < 90 else 'stalled?'), f'{last} (log updated {age:.0f} min ago)'


def _least_moves_section(workers: int):
    st.subheader('Least-moves layout — the same plan, fewest moves (background)')
    st.caption('Keeps the plan exactly (fish per batch per week, arrivals, 6N staging, harvest) and lays it in the '
               'tanks with as few moves as possible. About 2.5 hours; it runs in the background, so you can keep '
               'using the app or close it. The PC must stay awake.')
    up = st.file_uploader('Planned workbook', type=['xlsm', 'xlsx'], key='tl_lm_wb')
    if up is not None and st.button('▶ Start least-moves', key='tl_lm_go'):
        src = _save_upload(up)
        d = JOBS / f"lm_{datetime.now():%Y%m%d_%H%M%S}"; d.mkdir(parents=True)
        out = d / f'{src.stem}_least_moves.xlsx'
        with open(d / 'run.log', 'w') as lg:
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if sys.platform == 'win32' else 0
            subprocess.Popen([sys.executable, '-m', 'tools.least_moves', '--workbook', str(src), '--out', str(out),
                              '--workers', str(workers)], cwd=str(ROOT), stdout=lg, stderr=subprocess.STDOUT,
                             creationflags=flags)
        (d / 'source.txt').write_text(up.name)
        st.success('Started. It shows below; come back later.')
    jobs = sorted(JOBS.glob('lm_*'), reverse=True) if JOBS.exists() else []
    for d in jobs[:10]:
        state, last = _job_status(d)
        src = (d / 'source.txt').read_text() if (d / 'source.txt').exists() else '?'
        icon = {'done': '✅', 'failed': '❌', 'running': '⏳'}.get(state, '⚠️')
        with st.expander(f"{icon} {d.name[3:]} — {src} — {state}", expanded=(state == 'running')):
            st.code(last or '(no output yet)')
            out = next(iter(d.glob('*_least_moves.xlsx')), None)
            if out is not None:
                st.download_button('⬇ Least-moves workbook', out.read_bytes(), file_name=out.name,
                                   key=f'tl_lm_dl_{d.name}')


def render(workers: int):
    st.header('🧩 Tank layouts — fewer moves')
    st.caption('Two companion tools for a finished plan. Neither changes the plan, the config or any report: each '
               'writes its own workbook. See the user guide, "Least-moves layout" and "Tank rota".')
    _rota_section(workers)
    st.divider()
    _least_moves_section(workers)
