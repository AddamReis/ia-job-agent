const BUCKETS = [
  ['approval', 'Aguardando Aprovação'],
  ['pending', 'A Classificar'],
  ['submitted', 'Enviados'],
  ['rejected', 'Recusado'],
  ['errors', 'Erros'],
  ['all', 'Todas'],
];

const state = {
  bucket: 'approval',
  q: '',
  sort: 'newer',
  page: 1,
  items: [],
  counts: {},
  total: 0,
  pages: 0,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function fmtDate(value) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' });
  } catch { return String(value); }
}

function pill(text, kind = 'info') {
  return el('span', `pill ${kind}`, text);
}

async function load() {
  const list = $('#list');
  list.replaceChildren(el('div', 'empty', 'Carregando…'));
  try {
    const params = new URLSearchParams({
      bucket: state.bucket,
      q: state.q,
      sort: state.sort,
      page: String(state.page),
    });
    const data = await api(`/api/jobs?${params}`);
    state.items = data.items || [];
    state.counts = data.counts || {};
    state.total = data.total || 0;
    state.pages = data.pages || 0;
    state.page = data.page || 1;
    renderCounts();
    renderList();
    renderPager();
    $('#subtitle').textContent = `Atualizado às ${new Date().toLocaleTimeString('pt-BR')}`;
  } catch (err) {
    list.replaceChildren(el('div', 'empty', `Erro: ${err.message}`));
    renderPager();
  }
}

function renderPager() {
  const info = $('#page-info');
  const prev = $('#prev');
  const next = $('#next');
  if (!state.pages) {
    info.textContent = '—';
    prev.disabled = true;
    next.disabled = true;
    return;
  }
  info.textContent = `Página ${state.page} de ${state.pages} · ${state.total} ${state.total === 1 ? 'vaga' : 'vagas'}`;
  prev.disabled = state.page <= 1;
  next.disabled = state.page >= state.pages;
}

function renderCounts() {
  for (const [key, label] of BUCKETS) {
    const option = $(`#bucket option[value="${key}"]`);
    if (option) option.textContent = `${label} (${state.counts[key] ?? 0})`;
  }
}

function statusKind(status) {
  if (status === 'SUBMITTED') return 'ok';
  if (status === 'FAILED') return 'no';
  if (['QUALIFIED', 'PENDING_APPROVAL', 'APPLYING', 'WAITING_DEVICE', 'REQUIRES_HUMAN_ACTION'].includes(status)) return 'warn';
  return 'info';
}

function renderList() {
  const list = $('#list');
  list.replaceChildren();
  if (!state.items.length) {
    list.append(el('div', 'empty', 'Nenhuma vaga nesta lista.'));
    return;
  }
  for (const job of state.items) {
    const card = el('article', 'card');
    card.append(el('div', 'company', job.company || 'Empresa não informada'));
    card.append(el('h2', null, job.title || 'Sem título'));
    const meta = el('div', 'meta');
    meta.append(pill(job.status || '—', statusKind(job.status)));
    meta.append(pill(`Score ${job.score ?? '—'}`, job.score >= 70 ? 'ok' : 'warn'));
    meta.append(pill(job.decision || 'SEM DECISÃO', job.decision === 'APPLY' ? 'ok' : 'warn'));
    if (job.bucket === 'rejected') {
      for (const tech of job.missing_core || []) {
        meta.append(pill(`Falta ${tech}`, 'no'));
      }
    }
    meta.append(document.createTextNode(`${job.location || '—'} · ${fmtDate(job.posted_at)}`));
    card.append(meta);
    card.append(el('div', 'reason', job.reason || ''));
    card.addEventListener('click', () => openDetail(job.id));
    list.append(card);
  }
}

async function openDetail(id) {
  const sheet = $('#sheet');
  const detail = $('#detail');
  detail.replaceChildren(el('div', 'empty', 'Carregando…'));
  sheet.hidden = false;
  document.body.style.overflow = 'hidden';
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(id)}`);
    renderDetail(data);
  } catch (err) {
    detail.replaceChildren(el('div', 'empty', `Erro: ${err.message}`));
  }
}

function closeDetail() {
  $('#sheet').hidden = true;
  document.body.style.overflow = '';
}

function section(title) {
  return el('h3', null, title);
}

function kv(pairs) {
  const dl = el('dl', 'kv');
  for (const [key, value] of pairs) {
    dl.append(el('dt', null, key), el('dd', null, value ?? '—'));
  }
  return dl;
}

function chips(values) {
  const box = el('div', 'chips');
  if (!values || !values.length) box.append(el('span', 'chip', '—'));
  for (const value of values) box.append(el('span', 'chip', value));
  return box;
}

function renderDetail(data) {
  const root = $('#detail');
  root.replaceChildren();
  const job = data.job || {};
  const q = job.qualification || {};
  const a = job.analysis || {};

  root.append(el('h2', null, job.title || 'Sem título'));
  root.append(el('p', 'company', job.company || 'Empresa não informada'));
  root.append(kv([
    ['Status', job.status],
    ['Score', job.score],
    ['Decisão', q.decision],
    ['Local', job.location_normalized || job.location],
    ['Publicada', fmtDate(job.posted_at)],
    ['Moeda', job.currency || job.currency_signal || 'não informada'],
    ['Aprovação', data.approval ? data.approval.status : 'sem solicitação'],
  ]));
  if (data.automation_error) {
    root.append(el('p', 'reason error', `Erro da automação: ${data.automation_error}`));
  }

  root.append(section('Verificações'));
  const checks = el('div', 'checks');
  for (const [key, value] of Object.entries(data.checks || {})) {
    const row = el('div', 'check');
    row.append(el('span', null, `${value.ok ? '✓' : '✗'} ${key}`));
    row.append(el('span', null, value.detail));
    checks.append(row);
  }
  root.append(checks);

  root.append(section('Classificação'));
  root.append(kv([
    ['Motivo', q.hard_reject_reason || (q.reasons || []).join(' · ')],
    ['Breakdown', Object.entries(q.breakdown || {}).map(([k, v]) => `${k}: ${v}`).join(' · ')],
  ]));

  root.append(section('Aderência ao perfil'));
  root.append(kv([
    ['Resumo', a.summary],
    ['Senioridade', a.seniority],
    ['Anos exigidos', a.years_required],
    ['Remoto', a.is_remote === true ? 'sim' : a.is_remote === false ? 'não' : 'não informado'],
    ['Idiomas', (a.language_requirements || []).join(', ')],
    ['Red flags', (a.red_flags || []).join(' · ')],
  ]));
  root.append(section('Skills exigidas'));
  root.append(chips(a.required_skills));
  root.append(section('Skills desejáveis'));
  root.append(chips(a.preferred_skills));
  if ((data.missing_core || []).length) {
    root.append(section('Core ausente'));
    root.append(chips(data.missing_core));
  }

  root.append(section('Formulário preparado'));
  const answers = (data.approval && data.approval.answers) || [];
  root.append(kv(answers.length
    ? answers.map((ans) => [ans.field || 'campo', `${ans.value_redacted || '—'} (${ans.source || '—'})`])
    : [['Estado', data.approval ? 'sem respostas registradas' : 'ainda não preparado']]));

  if (job.url) {
    root.append(section('Vaga'));
    const link = el('a', 'link', 'Abrir publicação');
    link.href = job.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    root.append(link);
  }

  const actions = el('div', 'actions');
  const approve = el('button', 'btn approve', 'Aprovar e autorizar Apply');
  const reject = el('button', 'btn reject', 'Rejeitar');
  approve.disabled = !data.can_check;
  reject.disabled = !data.can_check;
  if (!data.can_check) {
    root.append(el('p', 'reason', 'O formulário ainda não foi preparado pelo agente. O check aparece assim que ele estiver pronto.'));
  }
  approve.addEventListener('click', () => decide(job.id, 'approve'));
  reject.addEventListener('click', () => decide(job.id, 'reject'));
  actions.append(approve, reject);
  root.append(actions);
}

async function decide(id, action) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: '{}' });
    toast(action === 'approve' ? 'Aprovado — agente autorizado.' : 'Rejeitado e registrado.');
    closeDetail();
    await load();
  } catch (err) {
    toast(err.message);
  }
}

let toastTimer;
function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
}

function reloadFromStart() {
  state.page = 1;
  load();
}

$('#bucket').addEventListener('change', (ev) => {
  state.bucket = ev.target.value;
  reloadFromStart();
});

let searchTimer;
$('#search').addEventListener('input', (ev) => {
  state.q = ev.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(reloadFromStart, 300);
});

$('#sort').addEventListener('change', (ev) => {
  state.sort = ev.target.value;
  reloadFromStart();
});

$('#prev').addEventListener('click', () => {
  if (state.page > 1) {
    state.page -= 1;
    load();
  }
});

$('#next').addEventListener('click', () => {
  if (state.page < state.pages) {
    state.page += 1;
    load();
  }
});

$('#refresh').addEventListener('click', load);
document.querySelectorAll('[data-close]').forEach((node) => node.addEventListener('click', closeDetail));
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closeDetail(); });

if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

load();
