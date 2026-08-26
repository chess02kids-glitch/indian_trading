"""Research cockpit HTML generation.

Generates a self-contained single-page HTML application for the quant
research cockpit. No external JS frameworks — vanilla JS with fetch()
for API calls.
"""

from __future__ import annotations

import json
from typing import Any


def render_cockpit_page(
    strategies: dict[str, dict[str, Any]],
    data_status: dict[str, Any],
) -> bytes:
    """Render the complete research cockpit HTML page."""
    strategies_json = json.dumps(strategies, sort_keys=True)
    data_status_json = json.dumps(data_status, sort_keys=True, default=str)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quant India — Research Cockpit</title>
<style>
:root {{--
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --accent: #58a6ff;
  --green: #3fb950;
  --red: #f85149;
  --yellow: #d29922;
  --orange: #db6d28;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  padding: 0;
}}
.header {{
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 12px 24px;
  display: flex;
  align-items: center;
  gap: 16px;
}}
.header h1 {{ font-size: 18px; font-weight: 600; }}
.header .subtitle {{ color: var(--text-dim); font-size: 13px; }}
.tabs {{
  display: flex; gap: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
}}
.tab {{
  padding: 10px 20px; cursor: pointer;
  border-bottom: 2px solid transparent;
  color: var(--text-dim); font-size: 14px;
  transition: all 0.15s;
}}
.tab:hover {{ color: var(--text); }}
.tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.content {{ padding: 24px; max-width: 1400px; margin: 0 auto; }}
.panel {{ display: none; }}
.panel.active {{ display: block; }}
.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 20px;
  margin-bottom: 16px;
}}
.card h2 {{ font-size: 16px; margin-bottom: 12px; }}
.card h3 {{ font-size: 14px; margin-bottom: 8px; color: var(--text-dim); }}
.grid {{ display: grid; gap: 16px; }}
.grid-2 {{ grid-template-columns: 1fr 1fr; }}
.grid-3 {{ grid-template-columns: 1fr 1fr 1fr; }}
.grid-4 {{ grid-template-columns: 1fr 1fr 1fr 1fr; }}
label {{ display: block; font-size: 13px; color: var(--text-dim); margin-bottom: 4px; }}
input, select {{
  background: var(--bg); border: 1px solid var(--border);
  color: var(--text); padding: 6px 10px;
  border-radius: 4px; font-size: 14px; width: 100%;
}}
input:focus, select:focus {{ outline: none; border-color: var(--accent); }}
.form-group {{ margin-bottom: 12px; }}
.btn {{
  padding: 8px 20px; border-radius: 4px;
  border: 1px solid var(--border); cursor: pointer;
  font-size: 14px; font-weight: 500;
}}
.btn-primary {{ background: #238636; color: white; border-color: #238636; }}
.btn-primary:hover {{ background: #2ea043; }}
.btn-primary:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.btn-secondary {{ background: var(--surface); color: var(--text); }}
.metric {{ text-align: center; padding: 12px; }}
.metric .value {{ font-size: 22px; font-weight: 600; }}
.metric .label {{ font-size: 12px; color: var(--text-dim); margin-top: 4px; }}
.verdict {{
  font-size: 28px; font-weight: 700;
  padding: 16px; text-align: center;
  border-radius: 6px; margin-bottom: 16px;
}}
.verdict.PASS {{ background: #0d2818; color: var(--green); border: 1px solid #238636; }}
.verdict.FAIL {{ background: #2d1214; color: var(--red); border: 1px solid #da3633; }}
.verdict.FRAGILE {{ background: #2d2205; color: var(--yellow); border: 1px solid #9e6a03; }}
.verdict.INSUFFICIENT_EVIDENCE {{ background: #1c2228; color: var(--text-dim); border: 1px solid var(--border); }}
.check-row {{
  display: flex; align-items: center; gap: 10px;
  padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px;
}}
.check-row:last-child {{ border-bottom: none; }}
.check-status {{
  display: inline-block; padding: 2px 8px;
  border-radius: 10px; font-size: 11px; font-weight: 600;
  min-width: 50px; text-align: center;
}}
.check-status.pass {{ background: #0d2818; color: var(--green); }}
.check-status.warn {{ background: #2d2205; color: var(--yellow); }}
.check-status.fail {{ background: #2d1214; color: var(--red); }}
.check-name {{ font-weight: 600; min-width: 140px; }}
.check-message {{ color: var(--text-dim); flex: 1; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ color: var(--text-dim); font-weight: 600; font-size: 12px; }}
.badge {{
  display: inline-block; padding: 2px 8px;
  border-radius: 10px; font-size: 11px; font-weight: 600;
}}
.badge.accepted {{ background: #0d2818; color: var(--green); }}
.badge.rejected {{ background: #2d1214; color: var(--red); }}
.badge.PASS {{ background: #0d2818; color: var(--green); }}
.badge.FAIL {{ background: #2d1214; color: var(--red); }}
.badge.FRAGILE {{ background: #2d2205; color: var(--yellow); }}
.status-dot {{
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 6px;
}}
.status-dot.green {{ background: var(--green); }}
.status-dot.red {{ background: var(--red); }}
.status-dot.gray {{ background: var(--text-dim); }}
.loading {{ display: none; text-align: center; padding: 40px; color: var(--text-dim); }}
.loading.active {{ display: block; }}
.spinner {{
  display: inline-block; width: 24px; height: 24px;
  border: 3px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 0.8s linear infinite;
  margin-bottom: 12px;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.error {{
  background: #2d1214; border: 1px solid #da3633;
  color: var(--red); padding: 12px 16px;
  border-radius: 6px; margin-bottom: 16px;
}}
.info {{
  background: #0d1d30; border: 1px solid #1f6feb;
  color: var(--accent); padding: 12px 16px;
  border-radius: 6px; margin-bottom: 16px; font-size: 13px;
}}
.rejection-reason {{
  background: #2d1214; border: 1px solid #da3633;
  padding: 12px 16px; border-radius: 6px;
  margin-bottom: 16px; font-size: 14px;
}}
.rejection-reason strong {{ color: var(--red); }}
.strat-desc {{ color: var(--text-dim); font-size: 13px; margin-bottom: 12px; font-style: italic; }}
.help-text {{ font-size: 11px; color: var(--text-dim); margin-top: 2px; }}
.score-bar {{ background: var(--border); border-radius: 4px; height: 8px; overflow: hidden; margin: 8px 0; }}
.score-fill {{ height: 100%; border-radius: 4px; }}
.param-section {{ border-left: 2px solid var(--accent); padding-left: 16px; margin-bottom: 16px; }}
code {{ background: var(--bg); padding: 2px 6px; border-radius: 3px; font-size: 13px; }}
@media (max-width: 768px) {{ .grid-2, .grid-3, .grid-4 {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>

<div class="header">
  <h1>Quant India</h1>
  <span class="subtitle">Research Cockpit</span>
  <span style="margin-left:auto;"><a href="/operations" style="color:var(--text-dim);font-size:13px;text-decoration:none;">Operations →</a></span>
</div>

<div class="tabs">
  <div class="tab active" data-panel="data">Data</div>
  <div class="tab" data-panel="run">Run Research</div>
  <div class="tab" data-panel="results">Results</div>
  <div class="tab" data-panel="history">Experiments</div>
</div>

<div class="content">
  <!-- DATA PANEL -->
  <div class="panel active" id="panel-data">
    <div class="card">
      <h2>Data Status</h2>
      <div id="data-status-content">Loading...</div>
    </div>
    <div class="card">
      <h2>Available Universes</h2>
      <div id="universe-content">Loading...</div>
    </div>
  </div>

  <!-- RUN PANEL -->
  <div class="panel" id="panel-run">
    <div class="grid grid-2">
      <div class="card">
        <h2>Strategy</h2>
        <div class="form-group">
          <label>Strategy</label>
          <select id="strategy-select"></select>
        </div>
        <div id="strategy-desc" class="strat-desc"></div>
        <div id="strategy-params"></div>
      </div>
      <div class="card">
        <h2>Configuration</h2>
        <div class="form-group">
          <label>Data Source</label>
          <select id="data-source">
            <option value="file">Price file (data/clean/prices.parquet)</option>
            <option value="synthetic">Synthetic data (testing only)</option>
          </select>
        </div>
        <div class="form-group" id="prices-path-group">
          <label>Prices file path</label>
          <input type="text" id="prices-path" value="data/clean/prices.parquet">
        </div>
        <div class="form-group">
          <label>Random seed</label>
          <input type="number" id="seed" value="42" min="0">
        </div>
        <h3 style="margin-top:16px;">Walk-Forward Validation</h3>
        <div class="grid grid-2">
          <div class="form-group">
            <label>Train size (days)</label>
            <input type="number" id="train-size" value="252" min="30">
          </div>
          <div class="form-group">
            <label>Test size (days)</label>
            <input type="number" id="test-size" value="63" min="10">
          </div>
        </div>
        <div class="form-group">
          <label style="display:flex;align-items:center;gap:6px;color:var(--text);">
            <input type="checkbox" id="expanding" style="width:auto;margin-right:6px;">
            Expanding window
          </label>
        </div>
        <div class="form-group">
          <label>Placebo samples</label>
          <input type="number" id="placebo-samples" value="50" min="10" max="500">
        </div>
      </div>
    </div>
    <div style="text-align:center;margin:20px 0;">
      <button class="btn btn-primary" id="run-btn" onclick="launchRun()">Run Research Experiment</button>
    </div>
    <div class="loading" id="run-loading">
      <div class="spinner"></div>
      <div>Running research pipeline...</div>
      <div style="font-size:12px;margin-top:8px;">Backtest → Walk-forward → Placebo → Gate evaluation</div>
    </div>
    <div id="run-error"></div>
  </div>

  <!-- RESULTS PANEL -->
  <div class="panel" id="panel-results">
    <div id="results-content">
      <div class="info">Run a research experiment to see results here.</div>
    </div>
  </div>

  <!-- HISTORY PANEL -->
  <div class="panel" id="panel-history">
    <div class="card">
      <h2>Experiment History</h2>
      <div id="history-content">Loading...</div>
    </div>
  </div>
</div>

<script>
const STRATEGIES = {strategies_json};
const DATA_STATUS = {data_status_json};
let lastResult = null;

// Tabs
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + tab.dataset.panel).classList.add('active');
    if (tab.dataset.panel === 'history') loadHistory();
  }});
}});

// Data panel
function renderDataStatus() {{
  const ds = DATA_STATUS;
  let html = '<div class="grid grid-3">';
  const hasData = ds.prices_exists;
  html += '<div class="metric"><div class="value"><span class="status-dot ' + (hasData ? 'green' : 'red') + '"></span>' + (hasData ? 'Available' : 'Missing') + '</div><div class="label">Price Data</div>';
  if (hasData && ds.prices_info) {{
    html += '<div style="font-size:12px;color:var(--text-dim);margin-top:8px;">' + ds.prices_info.dates + ' dates × ' + ds.prices_info.symbols + ' symbols<br>' + ds.prices_info.date_range + '</div>';
  }}
  if (ds.prices_size_mb) html += '<div style="font-size:11px;color:var(--text-dim);">' + ds.prices_size_mb + ' MB</div>';
  html += '</div>';
  html += '<div class="metric"><div class="value" style="font-size:14px;word-break:break-all;">' + ds.prices_file + '</div><div class="label">Prices File</div></div>';
  const uniCount = Object.values(ds.universe_files || {{}}).filter(u => u.exists).length;
  html += '<div class="metric"><div class="value">' + uniCount + '</div><div class="label">Universe Files</div></div></div>';
  if (!hasData) {{
    html += '<div class="info" style="margin-top:16px;"><strong>No price data found.</strong> Run data ingestion first:<br><code style="display:block;margin-top:8px;padding:8px;">python main.py ingest --symbol RELIANCE.NS<br># or use synthetic data for testing (select in Run panel)</code></div>';
  }}
  document.getElementById('data-status-content').innerHTML = html;
  let uniHtml = '<table><tr><th>Universe</th><th>Status</th><th>Path</th></tr>';
  for (const [name, info] of Object.entries(ds.universe_files || {{}})) {{
    uniHtml += '<tr><td>' + name + '</td><td><span class="status-dot ' + (info.exists ? 'green' : 'red') + '"></span>' + (info.exists ? 'Available' : 'Missing') + '</td><td style="font-size:12px;color:var(--text-dim)">' + info.path + '</td></tr>';
  }}
  document.getElementById('universe-content').innerHTML = uniHtml + '</table>';
}}

// Strategy selection
function populateStrategies() {{
  const select = document.getElementById('strategy-select');
  for (const [key, strat] of Object.entries(STRATEGIES)) {{
    const opt = document.createElement('option');
    opt.value = key; opt.textContent = strat.label;
    select.appendChild(opt);
  }}
  select.addEventListener('change', renderStrategyParams);
  renderStrategyParams();
}}

function renderStrategyParams() {{
  const key = document.getElementById('strategy-select').value;
  const strat = STRATEGIES[key];
  document.getElementById('strategy-desc').textContent = strat.description;
  let html = '<div class="param-section">';
  for (const [pKey, param] of Object.entries(strat.parameters)) {{
    html += '<div class="form-group"><label>' + param.label + '</label>';
    if (param.type === 'choice') {{
      html += '<select id="param-' + pKey + '">';
      for (const opt of param.options) html += '<option value="' + opt + '"' + (opt === param.default ? ' selected' : '') + '>' + opt + '</option>';
      html += '</select>';
    }} else if (param.type === 'bool') {{
      html += '<label style="display:flex;align-items:center;gap:6px;color:var(--text);"><input type="checkbox" id="param-' + pKey + '"' + (param.default ? ' checked' : '') + ' style="width:auto;"> ' + param.label + '</label>';
    }} else {{
      const step = param.step || (param.type === 'float' ? '0.01' : '1');
      html += '<input type="number" id="param-' + pKey + '" value="' + param.default + '" step="' + step + '"';
      if (param.min !== undefined) html += ' min="' + param.min + '"';
      if (param.max !== undefined) html += ' max="' + param.max + '"';
      html += '>';
    }}
    if (param.help) html += '<div class="help-text">' + param.help + '</div>';
    html += '</div>';
  }}
  document.getElementById('strategy-params').innerHTML = html + '</div>';
}}

document.getElementById('data-source').addEventListener('change', function() {{
  document.getElementById('prices-path-group').style.display = this.value === 'file' ? 'block' : 'none';
}});

// Launch run
async function launchRun() {{
  const btn = document.getElementById('run-btn');
  const loading = document.getElementById('run-loading');
  const errorDiv = document.getElementById('run-error');
  btn.disabled = true; loading.classList.add('active'); errorDiv.innerHTML = '';
  const strategy = document.getElementById('strategy-select').value;
  const dataSource = document.getElementById('data-source').value;
  const strat = STRATEGIES[strategy];
  const params = {{}};
  for (const [pKey, param] of Object.entries(strat.parameters)) {{
    const el = document.getElementById('param-' + pKey);
    if (param.type === 'bool') params[pKey] = el.checked;
    else if (param.type === 'int') params[pKey] = parseInt(el.value);
    else if (param.type === 'float') params[pKey] = parseFloat(el.value);
    else params[pKey] = el.value;
  }}
  const payload = {{
    strategy, parameters: params,
    use_synthetic: dataSource === 'synthetic',
    prices_path: document.getElementById('prices-path').value,
    train_size: parseInt(document.getElementById('train-size').value),
    test_size: parseInt(document.getElementById('test-size').value),
    expanding: document.getElementById('expanding').checked,
    placebo_samples: parseInt(document.getElementById('placebo-samples').value),
    seed: parseInt(document.getElementById('seed').value),
  }};
  try {{
    const resp = await fetch('/api/research/run', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(payload),
    }});
    if (!resp.ok) {{
      const errData = await resp.json().catch(() => ({{error: resp.statusText}}));
      throw new Error(errData.error || 'Request failed');
    }}
    lastResult = await resp.json();
    renderResults(lastResult);
    document.querySelector('.tab[data-panel="results"]').click();
  }} catch (err) {{
    errorDiv.innerHTML = '<div class="error">Error: ' + escHtml(err.message) + '</div>';
  }} finally {{
    btn.disabled = false; loading.classList.remove('active');
  }}
}}

// Render results
function renderResults(r) {{
  if (!r) {{ document.getElementById('results-content').innerHTML = '<div class="info">Run a research experiment to see results here.</div>'; return; }}
  let h = '';
  // Verdict
  h += '<div class="verdict ' + r.verdict + '">' + r.verdict + '</div>';
  h += '<div style="text-align:center;margin-bottom:16px;"><span style="color:var(--text-dim);">Gate Score: </span><strong>' + r.score.toFixed(1) + '/100</strong>';
  const sc = r.score >= 70 ? 'var(--green)' : (r.score >= 40 ? 'var(--yellow)' : 'var(--red)');
  h += '<div class="score-bar" style="max-width:300px;margin:8px auto;"><div class="score-fill" style="width:' + r.score + '%;background:' + sc + ';"></div></div></div>';
  if (r.rejection_reason) h += '<div class="rejection-reason"><strong>REJECTION REASON:</strong><br>' + escHtml(r.rejection_reason) + '</div>';
  // Strategy
  h += '<div class="card"><h2>Strategy: ' + escHtml(r.strategy) + '</h2><div style="font-size:13px;color:var(--text-dim);">';
  for (const [k,v] of Object.entries(r.parameters||{{}})) h += '<span style="margin-right:16px;">' + k + ': <strong>' + v + '</strong></span>';
  h += '</div></div>';
  // Metrics
  const m = r.metrics || {{}};
  h += '<div class="card"><h2>Performance Metrics</h2><div class="grid grid-4">';
  const defs = [
    ['total_return','Total Return','pct'],['annualized_return','CAGR','pct'],
    ['sharpe','Sharpe','f2'],['sortino','Sortino','f2'],
    ['annualized_volatility','Volatility','pct'],['max_drawdown','Max Drawdown','pct'],
    ['calmar','Calmar','f2'],['turnover','Turnover','f2'],
    ['win_rate','Win Rate','pct'],['trade_count','Trades','int'],
    ['cost_drag','Cost Drag','pct'],['observations','Observations','int'],
  ];
  for (const [key,label,fmt] of defs) {{
    let val = m[key], d = '—';
    if (val !== undefined && val !== null) {{
      if (fmt==='pct') d=(val*100).toFixed(2)+'%';
      else if (fmt==='f2') d=val.toFixed(2);
      else if (fmt==='int') d=Math.round(val).toString();
      else d=String(val);
    }}
    const c = (key==='max_drawdown'&&val<-0.3)?'var(--red)':(key==='sharpe'&&val>1)?'var(--green)':'var(--text)';
    h += '<div class="metric"><div class="value" style="color:'+c+'">'+d+'</div><div class="label">'+label+'</div></div>';
  }}
  h += '</div></div>';
  // Gate checks
  h += '<div class="card"><h2>Gate Checks</h2>';
  for (const ck of (r.gate_checks||[])) {{
    h += '<div class="check-row"><span class="check-status '+ck.status+'">'+ck.status.toUpperCase()+'</span><span class="check-name">'+escHtml(ck.name)+'</span><span class="check-message">'+escHtml(ck.message)+'</span></div>';
  }}
  h += '</div>';
  // Walk-forward
  const v = r.validation || {{}};
  const folds = v.fold_metrics || [];
  h += '<div class="card"><h2>Walk-Forward Validation</h2>';
  if (folds.length) {{
    h += '<table><tr><th>Fold</th><th>Sharpe</th><th>Return</th><th>Max DD</th><th>Obs</th></tr>';
    for (let i=0;i<folds.length;i++) {{
      const f=folds[i];
      h += '<tr><td>'+(i+1)+'</td><td style="color:'+((f.sharpe||0)>0?'var(--green)':'var(--red)')+'">'+(f.sharpe||0).toFixed(3)+'</td>';
      h += '<td>'+((f.total_return||0)*100).toFixed(2)+'%</td>';
      h += '<td style="color:var(--red)">'+((f.max_drawdown||0)*100).toFixed(2)+'%</td>';
      h += '<td>'+(f.observations||'—')+'</td></tr>';
    }}
    h += '</table>';
  }}
  const c = r.consistency || {{}};
  if (Object.keys(c).length) {{
    h += '<div style="margin-top:12px;font-size:13px;"><strong>Consistency:</strong> '+((c.positive_fold_fraction||0)*100).toFixed(0)+'% positive folds ('+(c.folds||0)+' folds)';
    if (c.worst_fold_sharpe!==undefined) h += ' | Worst fold Sharpe: '+(c.worst_fold_sharpe||0).toFixed(3);
    h += '</div>';
  }}
  h += '</div>';
  // Benchmarks
  const bm = r.benchmarks || {{}};
  h += '<div class="card"><h2>Benchmark Comparison</h2>';
  if (Object.keys(bm).length) {{
    h += '<table><tr><th>Strategy</th><th>Sharpe</th><th>CAGR</th><th>Max DD</th><th>Vol</th></tr>';
    h += '<tr style="font-weight:600;"><td>'+escHtml(r.strategy)+'</td><td>'+(m.sharpe||0).toFixed(3)+'</td><td>'+((m.annualized_return||0)*100).toFixed(2)+'%</td><td>'+((m.max_drawdown||0)*100).toFixed(2)+'%</td><td>'+((m.annualized_volatility||0)*100).toFixed(2)+'%</td></tr>';
    for (const [name,met] of Object.entries(bm)) {{
      h += '<tr><td style="color:var(--text-dim)">'+escHtml(name)+'</td><td>'+(met.sharpe||0).toFixed(3)+'</td><td>'+((met.annualized_return||0)*100).toFixed(2)+'%</td><td>'+((met.max_drawdown||0)*100).toFixed(2)+'%</td><td>'+((met.annualized_volatility||0)*100).toFixed(2)+'%</td></tr>';
    }}
    h += '</table>';
  }}
  h += '</div>';
  // Equity curve
  if (r.equity_curve_data && r.equity_curve_data.length > 1) {{
    h += '<div class="card"><h2>Equity Curve</h2>' + sparkline(r.equity_curve_data,'var(--accent)') + '</div>';
    h += '<div class="card"><h2>Drawdown</h2>' + (r.drawdown_data ? sparkline(r.drawdown_data,'var(--red)') : '') + '</div>';
  }}
  document.getElementById('results-content').innerHTML = h;
}}

function sparkline(data, color) {{
  const vals = data.map(d=>d.value);
  const mn = Math.min(...vals), mx = Math.max(...vals), rng = mx-mn||1;
  const w=800, ht=150;
  const pts = vals.map((v,i) => (i/(vals.length-1))*w + ',' + (ht-((v-mn)/rng)*(ht-20)-10)).join(' ');
  let s = '<svg viewBox="0 0 '+w+' '+ht+'" style="width:100%;height:200px;">';
  s += '<polyline fill="none" stroke="'+color+'" stroke-width="2" points="'+pts+'"/>';
  if (mn<0) {{ const zy=ht-((0-mn)/rng)*(ht-20)-10; s += '<line x1="0" y1="'+zy+'" x2="'+w+'" y2="'+zy+'" stroke="var(--border)" stroke-dasharray="4"/>'; }}
  s += '<text x="0" y="'+ht+'" fill="var(--text-dim)" font-size="10">'+(data[0].date||'').substring(0,10)+'</text>';
  s += '<text x="'+w+'" y="'+ht+'" fill="var(--text-dim)" font-size="10" text-anchor="end">'+(data[data.length-1].date||'').substring(0,10)+'</text>';
  return s + '</svg>';
}}

// History
async function loadHistory() {{
  const c = document.getElementById('history-content');
  try {{
    const resp = await fetch('/api/research/experiments');
    const exps = await resp.json();
    if (!exps.length) {{ c.innerHTML='<div class="info">No experiments recorded yet.</div>'; return; }}
    let h='<table><tr><th>Run ID</th><th>Strategy</th><th>Status</th><th>Verdict</th><th>Reason</th><th>Time</th><th></th></tr>';
    for (const e of exps.reverse()) {{
      const sc = e.status==='accepted'?'accepted':'rejected';
      let vd='—', vc='';
      if (e.gate_result&&e.gate_result.verdict) {{ vd=e.gate_result.verdict; vc=vd; }}
      h += '<tr><td style="font-family:monospace;font-size:12px;">'+(e.run_id||e.hypothesis_id||'—')+'</td>';
      h += '<td>'+escHtml(e.strategy||'—')+'</td>';
      h += '<td><span class="badge '+sc+'">'+e.status+'</span></td>';
      h += '<td><span class="badge '+vc+'">'+vd+'</span></td>';
      h += '<td style="font-size:12px;color:var(--text-dim);max-width:300px;overflow:hidden;text-overflow:ellipsis;">'+escHtml(e.reason||'')+'</td>';
      h += '<td style="font-size:12px;color:var(--text-dim);">'+(e.ended_at||e.started_at||'').substring(0,16).replace('T',' ')+'</td>';
      h += '<td><button class="btn btn-secondary" style="padding:2px 8px;font-size:11px;" onclick="viewExp(\\''+escHtml(e.run_id||e.hypothesis_id)+'\\')">View</button></td></tr>';
    }}
    c.innerHTML = h + '</table>';
  }} catch(err) {{ c.innerHTML='<div class="error">'+escHtml(err.message)+'</div>'; }}
}}

async function viewExp(runId) {{
  try {{
    const resp = await fetch('/api/research/experiment/'+encodeURIComponent(runId));
    if (!resp.ok) throw new Error('Not found');
    const e = await resp.json();
    const r = {{
      run_id:e.run_id||e.hypothesis_id, strategy:e.strategy,
      parameters:e.parameters||{{}}, status:e.status,
      verdict:(e.gate_result||{{}}).verdict||(e.status==='accepted'?'PASS':'FAIL'),
      score:(e.gate_result||{{}}).score||0, metrics:e.metrics||{{}},
      gate_checks:(e.gate_result||{{}}).checks||[],
      validation:e.validation||{{}}, benchmarks:e.benchmarks||{{}},
      consistency:e.validation||{{}}, rejection_reason:e.reason,
      started_at:e.started_at, ended_at:e.ended_at,
    }};
    lastResult = r; renderResults(r);
    document.querySelector('.tab[data-panel="results"]').click();
  }} catch(err) {{ alert('Error: '+err.message); }}
}}

function escHtml(t) {{ if(!t)return''; const d=document.createElement('div'); d.textContent=String(t); return d.innerHTML; }}

// Init
renderDataStatus();
populateStrategies();
</script>
</body>
</html>"""

    return html.encode("utf-8")
