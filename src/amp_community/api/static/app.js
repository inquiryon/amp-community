'use strict';
/* AMP Community operator UI (OSS-005). Vanilla JS, no build step. */

const $ = (sel) => document.querySelector(sel);
const status = (msg, isError) => {
  const el = $('#status');
  el.textContent = msg || '';
  el.className = isError ? 'error' : '';
};

const token = () => sessionStorage.getItem('amp_token') || '';

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {
      'Authorization': 'Bearer ' + token(),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new Error(`${err.code || res.status}: ${err.message || 'request failed'}`
      + (err.details ? ' — ' + JSON.stringify(err.details) : ''));
  }
  return data;
}

function parseJsonField(text, label) {
  if (!text.trim()) return {};
  let value;
  try { value = JSON.parse(text); } catch (e) {
    throw new Error(`${label} is not valid JSON: ${e.message}`);
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value;
}

/* ---- tabs ---- */
document.querySelectorAll('nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach((b) => {
      b.classList.toggle('active', b === btn);
      b.toggleAttribute('aria-current', b === btn);
    });
    document.querySelectorAll('main > section').forEach((s) => {
      s.hidden = s.id !== 'tab-' + btn.dataset.tab;
    });
    status('');
  });
});

/* ---- identity ---- */
async function refreshWhoami() {
  if (!token()) { $('#whoami').textContent = 'no token set (Settings)'; return; }
  try {
    const me = await api('GET', '/api/v0/me');
    $('#whoami').textContent = `${me.principal_id} (${me.role})`;
  } catch (e) { $('#whoami').textContent = 'auth failed'; }
}
$('#save-token').addEventListener('click', () => {
  sessionStorage.setItem('amp_token', $('#token').value.trim());
  status('Token saved for this browser session.');
  refreshWhoami();
});
$('#retry-callbacks').addEventListener('click', async () => {
  try {
    const out = await api('POST', '/api/v0/callbacks/retry');
    status('Retry results: ' + JSON.stringify(out.results));
  } catch (e) { status(e.message, true); }
});

/* ---- worktray ---- */
let currentItem = null;

async function loadWorktray() {
  try {
    const out = await api('GET', '/api/v0/hitl');
    const list = $('#worktray-list');
    list.textContent = '';
    if (!out.items.length) {
      list.innerHTML = '<li class="empty">No pending reviews.</li>';
    }
    out.items.forEach((wi) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.textContent = `${wi.tool}/${wi.action} — ${wi.reason} (${wi.hitl_id})`;
      btn.addEventListener('click', () => showItem(wi));
      li.appendChild(btn);
      list.appendChild(li);
    });
    status(`${out.items.length} pending review(s).`);
  } catch (e) { status(e.message, true); }
}

function showItem(wi) {
  currentItem = wi;
  $('#workitem-detail').hidden = false;
  $('#wi-id').textContent = wi.hitl_id;
  $('#wi-fields').innerHTML = '';
  [['Request', wi.request_id], ['Tool', wi.tool], ['Action', wi.action],
   ['Reason', wi.reason], ['Triggered criteria', (wi.triggered_criteria || []).join(', ') || '—'],
   ['Requested by', wi.requested_by], ['Created', wi.created_at],
   ['Policy', wi.policy_id || '—']].forEach(([k, v]) => {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = v;
    $('#wi-fields').append(dt, dd);
  });
  $('#wi-params').textContent = JSON.stringify(wi.params, null, 2);
  $('#modified-params').value = JSON.stringify(wi.params, null, 2);
  $('#resolve-result').textContent = '';
}

$('#refresh-worktray').addEventListener('click', loadWorktray);
document.querySelectorAll('#resolve-form input[name=decision]').forEach((r) => {
  r.addEventListener('change', () => {
    $('#modify-area').hidden = r.value !== 'modify' || !r.checked;
  });
});

$('#resolve-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  if (!currentItem) return;
  const decision = new FormData(ev.target).get('decision');
  const body = { decision, note: $('#note').value };
  try {
    if (decision === 'modify') {
      body.modified_params = parseJsonField($('#modified-params').value,
        'Replacement parameters');
    }
    const out = await api('POST',
      `/api/v0/hitl/${encodeURIComponent(currentItem.hitl_id)}/resolve`, body);
    const r = out.result;
    let text = `Decision recorded: ${r.decision}.`;
    if (r.decision === 'modified') {
      text += r.executed
        ? ' Modified action passed re-evaluation and was released.'
        : ` Modified action did NOT pass re-evaluation — follow-up review ${r.follow_up_hitl_id} created; nothing was executed.`;
    }
    text += out.delivered ? ' Callback delivered.' : ' Callback pending/undelivered (see Settings → retry).';
    $('#resolve-result').textContent = text;
    $('#workitem-detail').hidden = true;
    loadWorktray();
    status(text);
  } catch (e) { status(e.message, true); }
});

/* ---- policies ---- */
async function loadPolicies() {
  try {
    const out = await api('GET', '/api/v0/policies');
    const list = $('#policy-list');
    list.textContent = '';
    out.items.forEach((id) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.textContent = id;
      btn.addEventListener('click', async () => {
        const policy = await api('GET', '/api/v0/policies/' + encodeURIComponent(id));
        $('#policy-id').value = id;
        $('#policy-json').value = JSON.stringify(policy, null, 2);
      });
      li.appendChild(btn);
      list.appendChild(li);
    });
  } catch (e) { status(e.message, true); }
}
$('#refresh-policies').addEventListener('click', loadPolicies);

$('#policy-validate').addEventListener('click', async () => {
  try {
    const doc = parseJsonField($('#policy-json').value, 'Policy document');
    const id = $('#policy-id').value.trim() || 'unsaved';
    const out = await api('POST',
      `/api/v0/policies/${encodeURIComponent(id)}/validate`, doc);
    $('#policy-result').textContent = out.valid
      ? 'Valid against the v0 Policy schema.'
      : 'Invalid: ' + out.errors.join(' | ');
  } catch (e) { status(e.message, true); }
});

$('#policy-save').addEventListener('click', async () => {
  try {
    const id = $('#policy-id').value.trim();
    if (!id) throw new Error('Policy id is required');
    const doc = parseJsonField($('#policy-json').value, 'Policy document');
    await api('PUT', '/api/v0/policies/' + encodeURIComponent(id), doc);
    $('#policy-result').textContent = `Saved ${id}.`;
    loadPolicies();
  } catch (e) { status(e.message, true); }
});

$('#policy-export').addEventListener('click', () => {
  const blob = new Blob([$('#policy-json').value], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = ($('#policy-id').value.trim() || 'policy') + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ---- tester ---- */
$('#t-run').addEventListener('click', async () => {
  try {
    const id = $('#t-policy').value.trim();
    if (!id) throw new Error('Policy id is required');
    const out = await api('POST',
      `/api/v0/policies/${encodeURIComponent(id)}/evaluate`, {
        tool: $('#t-tool').value.trim() || '*',
        action: $('#t-action').value.trim() || '*',
        params: parseJsonField($('#t-params').value, 'Params'),
      });
    $('#t-result').innerHTML = '';
    const verdict = document.createElement('p');
    verdict.className = 'verdict ' + out.decision;
    verdict.textContent = `${out.decision} — ${out.reason}`;
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(out.criteria_results, null, 2);
    $('#t-result').append(verdict, pre);
  } catch (e) { status(e.message, true); }
});

/* ---- audit ---- */
$('#a-query').addEventListener('click', async () => {
  try {
    const rid = $('#a-request').value.trim();
    const out = await api('GET',
      '/api/v0/audit' + (rid ? '?request_id=' + encodeURIComponent(rid) : ''));
    const tbody = $('#a-table tbody');
    tbody.textContent = '';
    out.items.forEach((e) => {
      const tr = document.createElement('tr');
      [e.occurred_at, e.event_type, e.request_id, e.actor,
       JSON.stringify(e.payload)].forEach((v) => {
        const td = document.createElement('td');
        td.textContent = v;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    status(`${out.items.length} event(s).`);
  } catch (e) { status(e.message, true); }
});

/* ---- init ---- */
refreshWhoami();
if (token()) loadWorktray();
