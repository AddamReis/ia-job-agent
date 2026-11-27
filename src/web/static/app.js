const BUCKETS = [
  ['approval', 'Aguardando Aprovação'],
  ['pending', 'A Classificar'],
  ['submitted', 'Enviados'],
  ['rejected', 'Recusado'],
  ['errors', 'Erros'],
  ['all', 'Todas'],
];

const DEFAULT_SORTS = {
  approval: 'classified_desc',
  submitted: 'classified_desc',
  rejected: 'classified_desc',
  errors: 'classified_desc',
  pending: 'created_desc',
  all: 'created_desc',
};

const state = {
  bucket: 'approval',
  q: '',
  sort: DEFAULT_SORTS.approval,
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

// Visualizador de evidências: amplia a imagem sem baixar e permite navegar
// entre os prints com setas laterais, teclado ou gesto de arrastar.
const lightboxState = { items: [], index: 0 };

function ensureLightbox() {
  let root = $('.lightbox');
  if (root) return root;
  root = el('div', 'lightbox');
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', 'Visualizador de evidências');
  root.hidden = true;

  const backdrop = el('div', 'lightbox-backdrop');
  const panel = el('div', 'lightbox-panel');
  const header = el('div', 'lightbox-header');
  const counter = el('span', 'lightbox-counter');
  const close = el('button', 'lightbox-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', 'Fechar visualizador');
  header.append(counter, close);

  const stage = el('div', 'lightbox-stage');
  const prev = el('button', 'lightbox-nav prev', '‹');
  prev.type = 'button';
  prev.setAttribute('aria-label', 'Evidência anterior');
  const img = el('img');
  img.alt = '';
  const next = el('button', 'lightbox-nav next', '›');
  next.type = 'button';
  next.setAttribute('aria-label', 'Próxima evidência');
  stage.append(prev, img, next);

  const caption = el('div', 'lightbox-caption');
  panel.append(header, stage, caption);
  root.append(backdrop, panel);
  document.body.append(root);

  root.parts = { counter, img, caption, prev, next, stage };
  close.addEventListener('click', closeLightbox);
  backdrop.addEventListener('click', closeLightbox);
  prev.addEventListener('click', () => moveLightbox(-1));
  next.addEventListener('click', () => moveLightbox(1));

  let touchStartX = null;
  stage.addEventListener('touchstart', (event) => {
    touchStartX = event.touches[0]?.clientX ?? null;
  }, { passive: true });
  stage.addEventListener('touchend', (event) => {
    if (touchStartX === null) return;
    const delta = (event.changedTouches[0]?.clientX ?? touchStartX) - touchStartX;
    touchStartX = null;
    if (Math.abs(delta) > 40) moveLightbox(delta < 0 ? 1 : -1);
  }, { passive: true });

  document.addEventListener('keydown', (event) => {
    if (root.hidden) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeLightbox();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      moveLightbox(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      moveLightbox(1);
    }
  });
  return root;
}

function renderLightbox() {
  const root = $('.lightbox');
  if (!root || root.hidden || !lightboxState.items.length) return;
  const { items, index } = lightboxState;
  const item = items[Math.max(0, Math.min(index, items.length - 1))];
  root.parts.img.src = item.href;
  root.parts.img.alt = item.label;
  root.parts.counter.textContent = `${index + 1} / ${items.length}`;
  root.parts.caption.textContent = item.caption;
  root.parts.prev.disabled = items.length < 2;
  root.parts.next.disabled = items.length < 2;
}

function openLightbox(items, index) {
  if (!items || !items.length) return;
  const root = ensureLightbox();
  lightboxState.items = items;
  lightboxState.index = index;
  root.hidden = false;
  document.body.classList.add('lightbox-open');
  renderLightbox();
  $('.lightbox .lightbox-close')?.focus();
}

function closeLightbox() {
  const root = $('.lightbox');
  if (!root) return;
  root.hidden = true;
  document.body.classList.remove('lightbox-open');
  root.parts.img.removeAttribute('src');
}

function moveLightbox(delta) {
  const total = lightboxState.items.length;
  if (total < 2) return;
  lightboxState.index = (lightboxState.index + delta + total) % total;
  renderLightbox();
}

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

  root.append(section('Formulário'));
  const answers = (data.approval && data.approval.answers) || [];
  root.append(kv(answers.length
    ? answers.map((ans) => [
        `Step ${ans.step || 1} · ${ans.field || 'campo'}`,
        `${ans.value_redacted || '—'} (${ans.source || '—'})`,
      ])
    : [['Estado', 'o agente prepara e preenche após sua autorização']]));

  const evidences = data.evidence || [];
  if (evidences.length) {
    root.append(section('Evidências do Apply'));
    const grid = el('div', 'evidence-grid');
    const galeria = [];
    for (const item of evidences) {
      const nome = String(item.path || '').split('/').pop();
      if (!nome) continue;
      const href = `/api/evidence/${encodeURIComponent(job.id)}/${encodeURIComponent(nome)}`;
      const rotulo = item.label || `Evidência step ${item.step || 1}`;
      const legendaTexto = `${item.kind === 'confirmation' ? 'Confirmação' : `Step ${item.step || 1}`} · ${rotulo}`;
      const indice = galeria.push({ href, label: rotulo, caption: legendaTexto }) - 1;

      const link = el('button', 'evidence-link');
      link.type = 'button';
      link.title = 'Ampliar evidência';
      link.setAttribute('aria-label', `Ampliar ${legendaTexto}`);
      const img = el('img');
      img.src = href;
      img.alt = rotulo;
      img.loading = 'lazy';
      link.append(img);
      link.addEventListener('click', () => openLightbox(galeria, indice));

      const legenda = el('figcaption', null, legendaTexto);
      const figure = el('figure', 'evidence');
      figure.append(link, legenda);
      grid.append(figure);
    }
    root.append(grid);
  }

  if (job.url) {
    root.append(section('Vaga'));
    const link = el('a', 'link', 'Abrir publicação');
    link.href = job.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    root.append(link);
  }

  const actions = el('div', 'actions');
  const approve = el('button', 'btn approve', 'Quero me candidatar — automatizar');
  const reject = el('button', 'btn reject', 'Rejeitar');
  approve.disabled = !data.can_check;
  reject.disabled = !data.can_check;
  if (!data.can_check) {
    root.append(el('p', 'reason', 'Vaga já enviada, recusada, em andamento ou com erro de automação.'));
  }
  approve.addEventListener('click', () => decide(job.id, 'approve'));
  reject.addEventListener('click', () => decide(job.id, 'reject'));
  actions.append(approve, reject);
  root.append(actions);
}

async function decide(id, action) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: '{}' });
    toast(action === 'approve'
      ? 'Autorizado — o agente vai preparar e enviar a candidatura.'
      : 'Rejeitado e registrado.');
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
  // Cada aba tem sua ordem natural: inclusão para descoberta/classificação,
  // classificação para decisão/envio/erro.
  state.sort = DEFAULT_SORTS[state.bucket] || 'created_desc';
  $('#sort').value = state.sort;
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
