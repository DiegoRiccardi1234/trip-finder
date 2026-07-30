'use strict';

const form = document.getElementById('search-form');
const statusBox = document.getElementById('status');
const phaseEl = document.getElementById('phase');
const countsEl = document.getElementById('counts');
const resultsEl = document.getElementById('results');
const stopBtn = document.getElementById('stop');
const goBtn = document.getElementById('go');
const saveBtn = document.getElementById('save-search');
const compareEl = document.getElementById('compare-advice');
const feedbackEl = document.getElementById('form-feedback');
const panelTemplate = document.getElementById('panel-template');
const tripTemplate = document.getElementById('trip-template');
const trattaTemplate = document.getElementById('tratta-template');

/** Un pannello per combinazione partenza × meta × data. */
const panels = [];

/** Quante colonne al massimo: oltre due le schede diventano illeggibili. */
const MAX_COLUMNS = 2;

const MODE_LABEL = {
  rail: 'treno', bus: 'pullman', air: 'aereo',
  ferry: 'nave', transfer: 'trasferimento', walk: 'a piedi',
};
const MODE_ICON = {
  rail: '🚆', bus: '🚌', air: '✈️', ferry: '⛴️', transfer: '🚕', walk: '🚶',
};
const KIND_ICON = {
  station: '🚉', airport: '✈️', bus_stop: '🚏', port: '⚓', city: '📍',
};
const FLAG_TEXT = {
  separate_tickets: ['danger', 'Biglietti separati: se salti una coincidenza nessuno ti riprotegge'],
  tight_connection: ['danger', 'Coincidenza stretta rispetto al margine consigliato'],
  station_change: ['', 'Cambio di stazione o scalo: serve spostarsi'],
  night_arrival: ['', 'Arrivo nel cuore della notte'],
  estimated_cost: ['info', 'Alcune voci di costo sono stimate'],
  last_leg_unverified: ['info', 'Ultima tratta da orario statico, non verificata live'],
};

/* ------------------------------------------------------------- utilità --- */

const pad = (n) => String(n).padStart(2, '0');

function fmtClock(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtDayOffset(startIso, endIso) {
  const a = new Date(startIso), b = new Date(endIso);
  const days = Math.round((b.setHours(0, 0, 0, 0) - a.setHours(0, 0, 0, 0)) / 86400000);
  return days > 0 ? `+${days}` : '';
}

function fmtDuration(minutes) {
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return h ? `${h}h ${pad(m)}` : `${m} min`;
}

function fmtMoney(value) {
  return `${value.toFixed(2).replace('.', ',')} €`;
}

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** Le tratte che si comprano davvero: i trasferimenti a terra ci sono sempre. */
function bookableLegs(itinerary) {
  return (itinerary?.legs || []).filter((leg) => leg.mode !== 'transfer' && leg.mode !== 'walk');
}

function say(message, kind = 'bad') {
  feedbackEl.hidden = !message;
  feedbackEl.className = `nl-feedback ${kind}`;
  feedbackEl.textContent = message;
}

/* ---------------------------------------------------------------- tema --- */

// Tre stati e non due: "auto" e' il default giusto (segue il sistema) e senza
// di lui, dopo il primo click, non ci si tornerebbe piu'.
const THEMES = ['auto', 'light', 'dark'];
const THEME_LABEL = { auto: '🌗 Tema: auto', light: '☀️ Tema: chiaro', dark: '🌙 Tema: scuro' };
const themeBtn = document.getElementById('theme');

function readTheme() {
  try {
    const saved = localStorage.getItem('tf-theme');
    return THEMES.includes(saved) ? saved : 'auto';
  } catch { return 'auto'; }
}

function applyTheme(theme) {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  themeBtn.textContent = THEME_LABEL[theme];
  themeBtn.title = theme === 'auto'
    ? 'Segue le impostazioni del sistema. Clicca per forzare chiaro o scuro.'
    : 'Clicca per cambiare tema.';
  try { localStorage.setItem('tf-theme', theme); } catch { /* niente memoria: vale per questa scheda */ }
}

themeBtn.addEventListener('click', () => {
  applyTheme(THEMES[(THEMES.indexOf(readTheme()) + 1) % THEMES.length]);
});
applyTheme(readTheme());

/* ---------------------------------------------------- autocompletamento --- */

const PLACE_FIELDS = ['origin', 'origin2', 'destination', 'destination2'];

function wireSuggest(inputName, listId) {
  const input = form.elements[inputName];
  const list = document.getElementById(listId);
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const value = input.value.trim();
    if (value.length < 2) return;
    timer = setTimeout(async () => {
      try {
        const response = await fetch(`/api/suggest?q=${encodeURIComponent(value)}`);
        if (!response.ok) return;
        const names = await response.json();
        list.innerHTML = names.map((n) => `<option value="${n.replace(/"/g, '&quot;')}">`).join('');
      } catch { /* l'autocompletamento è un lusso, non blocca nulla */ }
    }, 220);
  });
}
wireSuggest('origin', 'places-origin');
wireSuggest('origin2', 'places-origin2');
wireSuggest('destination', 'places-destination');
wireSuggest('destination2', 'places-destination2');

document.getElementById('swap').addEventListener('click', () => {
  const a = form.elements.origin, b = form.elements.destination;
  [a.value, b.value] = [b.value, a.value];
  loadStops('origin');
  loadStops('destination');
});

/* ------------------------------------------------------------- fermate --- */

// Una citta' si traduce in piu' fermate: Torino in Porta Nuova, Porta Susa,
// Stura, il terminal bus e Caselle. Il motore ne sceglie fino a due per
// operatore, e chi vuole restringere lo fa da qui. Nessuna scelta = tutte, che
// resta il comportamento normale.
const stops = new Map(PLACE_FIELDS.map((field) => [field, { query: '', nodes: [], chosen: new Set() }]));

function stopsBox(field) {
  return document.querySelector(`.stops[data-field="${field}"]`);
}

async function loadStops(field) {
  const state = stops.get(field);
  const query = form.elements[field].value.trim();
  if (query === state.query) return;
  state.query = query;
  state.nodes = [];
  state.chosen.clear();
  if (query.length < 2) { renderStops(field); return; }
  try {
    const response = await fetch(`/api/resolve?q=${encodeURIComponent(query)}`);
    if (!response.ok) { renderStops(field); return; }
    const place = await response.json();
    if (form.elements[field].value.trim() !== query) return;  // l'utente ha gia' riscritto
    state.nodes = place.nodes || [];
  } catch { /* senza rete si resta senza chip: la ricerca funziona lo stesso */ }
  renderStops(field);
}

function renderStops(field) {
  const box = stopsBox(field);
  const state = stops.get(field);
  if (!box) return;
  if (state.nodes.length < 2) { box.replaceChildren(); return; }
  const chips = state.nodes.map((node) => {
    const on = state.chosen.has(node.id);
    return `<button type="button" class="chip stop ${on ? 'on' : ''}" aria-pressed="${on}"`
      + ` data-node="${escapeHtml(node.id)}">`
      + `${KIND_ICON[node.kind] || '•'} ${escapeHtml(node.name)}</button>`;
  });
  const quante = state.chosen.size
    ? `<span class="hint">solo ${state.chosen.size} di ${state.nodes.length} — <a href="#" data-clear="1">tutte</a></span>`
    : `<span class="hint">${state.nodes.length} fermate, tutte in gara</span>`;
  box.innerHTML = chips.join('') + quante;
}

document.addEventListener('click', (event) => {
  const chip = event.target.closest('.stops .chip.stop');
  if (chip) {
    const field = chip.closest('.stops').dataset.field;
    const state = stops.get(field);
    const id = chip.dataset.node;
    if (state.chosen.has(id)) state.chosen.delete(id); else state.chosen.add(id);
    renderStops(field);
    return;
  }
  const clear = event.target.closest('.stops [data-clear]');
  if (clear) {
    event.preventDefault();
    const field = clear.closest('.stops').dataset.field;
    stops.get(field).chosen.clear();
    renderStops(field);
  }
});

for (const field of PLACE_FIELDS) {
  form.elements[field].addEventListener('change', () => loadStops(field));
  form.elements[field].addEventListener('blur', () => loadStops(field));
}

/* --------------------------------------------------- campi in piu' --------- */

const TOGGLES = {
  origin2: ['+ seconda partenza', '− togli la seconda partenza'],
  destination2: ['+ seconda meta', '− togli la seconda meta'],
  date2: ['+ seconda data', '− togli la seconda data'],
};

document.querySelectorAll('[data-toggle]').forEach((button) => {
  const name = button.dataset.toggle;
  const field = document.getElementById(`${name}-field`);
  button.addEventListener('click', () => {
    const showing = field.hidden;
    field.hidden = !showing;
    button.textContent = TOGGLES[name][showing ? 1 : 0];
    if (showing) form.elements[name].focus();
    else form.elements[name].value = '';
    say('');
  });
});

const returnField = document.getElementById('return-field');
form.elements.roundtrip.addEventListener('change', () => {
  returnField.hidden = !form.elements.roundtrip.checked;
  if (!returnField.hidden && !form.elements.return_date.value) {
    // Una settimana dopo l'andata: e' la durata tipica, e comunque si cambia.
    const andata = new Date(form.elements.date.value || Date.now());
    form.elements.return_date.value = new Date(andata.getTime() + 7 * 86400000)
      .toISOString().slice(0, 10);
  }
  syncReturnMin();
});

function syncReturnMin() {
  const dates = [form.elements.date.value, form.elements.date2.value].filter(Boolean);
  if (dates.length) form.elements.return_date.min = dates.sort().pop();
}
form.elements.date.addEventListener('change', syncReturnMin);
form.elements.date2.addEventListener('change', syncReturnMin);

/* ------------------------------------------- ricerca in linguaggio naturale */

const nlFeedback = document.getElementById('nl-feedback');
const interpretBtn = document.getElementById('interpret');

async function interpret() {
  const text = form.elements.nl.value.trim();
  if (text.length < 4) return;

  interpretBtn.disabled = true;
  nlFeedback.hidden = false;
  nlFeedback.className = 'nl-feedback';
  nlFeedback.textContent = 'Sto interpretando…';
  try {
    const response = await fetch(`/api/parse?text=${encodeURIComponent(text)}`, { method: 'POST' });
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      nlFeedback.className = 'nl-feedback bad';
      nlFeedback.textContent = problem.detail
        || 'Non sono riuscito a interpretarla. Compila i campi a mano.';
      return;
    }
    const q = await response.json();
    applyParsed(q);
    nlFeedback.className = 'nl-feedback ok';
    nlFeedback.textContent = describeParsed(q) + ' — controlla e premi Cerca.';
  } catch {
    nlFeedback.className = 'nl-feedback bad';
    nlFeedback.textContent = 'Interpretazione non riuscita.';
  } finally {
    interpretBtn.disabled = false;
  }
}

// La frase interpretata porta una combinazione sola: i campi in piu', se ci
// sono, restano quelli scritti a mano.
function applyParsed(q) {
  form.elements.origin.value = q.origin || '';
  form.elements.destination.value = q.destination || '';
  form.elements.date.value = q.date || form.elements.date.value;
  form.elements.pax.value = q.pax || 1;
  form.elements.bag.checked = Boolean(q.with_checked_bag);
  form.elements.allow_night.checked = q.allow_night !== false;
  form.elements.budget.value = q.max_budget ?? '';
  form.elements.depart_after.value = q.depart_after ? String(q.depart_after).slice(0, 5) : '';
  form.elements.arrive_by.value = q.arrive_by ? String(q.arrive_by).slice(0, 5) : '';
  const wanted = new Set(q.modes || []);
  document.querySelectorAll('input[name="mode"]').forEach((box) => {
    box.checked = wanted.size ? wanted.has(box.value) : true;
  });
  loadStops('origin');
  loadStops('destination');
}

function describeParsed(q) {
  const bits = [`${q.origin} → ${q.destination}`, `il ${fmtDate(q.date)}`];
  if (q.pax > 1) bits.push(`${q.pax} persone`);
  if (q.max_budget) bits.push(`max ${q.max_budget} €`);
  if (q.depart_after) bits.push(`partenza dopo le ${String(q.depart_after).slice(0, 5)}`);
  if (q.arrive_by) bits.push(`arrivo entro le ${String(q.arrive_by).slice(0, 5)}`);
  if (q.allow_night === false) bits.push('niente notturni');
  if (q.with_checked_bag) bits.push('con valigia');
  return bits.join(', ');
}

interpretBtn.addEventListener('click', interpret);
form.elements.nl.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); interpret(); }
});

/* --------------------------------------------- priorità (pesi + preset) --- */

const WEIGHT_KEYS = ['w_price', 'w_duration', 'w_risk', 'w_night', 'w_arrival', 'w_co2'];

// I preset toccano solo i pesi della classifica. "Niente notturni" resta la
// spunta `allow_night`, che e' un filtro: toglie soluzioni, non le declassa.
const PRESETS = {
  balanced: { w_price: 1.0, w_duration: 1.0, w_risk: 0.8, w_night: 0.5, w_arrival: 0.6, w_co2: 0.0 },
  cheap:    { w_price: 2.0, w_duration: 0.6, w_risk: 0.8, w_night: 0.8, w_arrival: 0.3, w_co2: 0.0 },
  fast:     { w_price: 0.5, w_duration: 2.0, w_risk: 1.0, w_night: 0.0, w_arrival: 0.8, w_co2: 0.0 },
  calm:     { w_price: 0.7, w_duration: 0.8, w_risk: 2.0, w_night: 0.0, w_arrival: 1.2, w_co2: 0.0 },
};

const weightsBox = document.getElementById('weights');
const weightsToggle = document.getElementById('toggle-weights');

weightsToggle.addEventListener('click', () => {
  weightsBox.hidden = !weightsBox.hidden;
  weightsToggle.setAttribute('aria-expanded', String(!weightsBox.hidden));
});

document.querySelectorAll('#weights input[type="range"]').forEach((slider) => {
  const output = slider.nextElementSibling;
  const sync = () => { output.textContent = Number(slider.value).toFixed(1); };
  slider.addEventListener('input', () => { sync(); markActivePreset(); });
  sync();
});

/** Quale preset descrive i cursori come stanno ora, se ce n'e' uno. */
function markActivePreset() {
  const current = Object.fromEntries(WEIGHT_KEYS.map((k) => [k, Number(form.elements[k].value)]));
  const match = Object.entries(PRESETS).find(([, values]) =>
    WEIGHT_KEYS.every((k) => Math.abs(values[k] - current[k]) < 0.001));
  document.querySelectorAll('.presets button').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(Boolean(match) && btn.dataset.preset === match[0]));
  });
  return match ? match[0] : '';
}

function applyPreset(name) {
  const values = PRESETS[name];
  if (!values) return;
  for (const key of WEIGHT_KEYS) {
    const slider = form.elements[key];
    slider.value = values[key];
    // L'evento riallinea l'<output> con la stessa funzione degli slider a
    // mano: la formattazione sta in un posto solo.
    slider.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

document.querySelectorAll('.presets button').forEach((btn) => {
  btn.addEventListener('click', () => applyPreset(btn.dataset.preset));
});
markActivePreset();

/** La data di oggi come la scrive un `<input type=date>`, nel fuso locale.
 *  Con `toISOString` fra mezzanotte e le due del mattino uscirebbe ieri. */
function oggiLocale() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const oggi = oggiLocale();
form.elements.date.value = oggi;
form.elements.date.min = oggi;
form.elements.date2.min = oggi;
form.elements.return_date.min = oggi;

/* -------------------------------------------------------- ordinamento --- */

const SORTERS = {
  score: (a, b) => b.score - a.score,
  price: (a, b) => a.total - b.total,
  duration: (a, b) => a.duration_min - b.duration_min,
  depart: (a, b) => new Date(a.depart) - new Date(b.depart),
  arrive: (a, b) => new Date(a.arrive) - new Date(b.arrive),
  changes: (a, b) => a.n_changes - b.n_changes,
};

/** L'ultima scelta vale anche per la ricerca dopo: chi ordina per prezzo lo fa di solito sempre. */
let lastSort = 'score';

/* ------------------------------------------------ stato nell'indirizzo --- */

// L'indirizzo della pagina porta tutta la ricerca: cosi' si mette nei
// preferiti, si manda a qualcuno e si salva nel profilo, che infatti conserva
// esattamente questa stringa.
const URL_FIELDS = [
  'origin', 'origin2', 'destination', 'destination2', 'date', 'date2',
  'return_date', 'pax', 'max_changes', 'budget', 'depart_after', 'arrive_by',
  ...WEIGHT_KEYS,
];

function currentParams() {
  const params = new URLSearchParams();
  for (const name of URL_FIELDS) {
    const value = form.elements[name].value;
    if (value) params.set(name, value);
  }
  for (const name of ['bag', 'allow_night', 'roundtrip']) {
    if (form.elements[name].checked !== (name === 'allow_night')) {
      params.set(name, String(form.elements[name].checked));
    }
  }
  const modes = [...form.querySelectorAll('input[name="mode"]:checked')].map((b) => b.value);
  if (modes.length !== 4) params.set('modes', modes.join(','));
  for (const [field, state] of stops) {
    if (state.chosen.size) params.set(`stops_${field}`, [...state.chosen].join(','));
  }
  for (const name of ['origin2', 'destination2', 'date2']) {
    if (!document.getElementById(`${name}-field`).hidden) params.set(`show_${name}`, '1');
  }
  if (lastSort !== 'score') params.set('sort', lastSort);
  return params;
}

function applyParams(params) {
  for (const name of URL_FIELDS) {
    if (params.has(name)) form.elements[name].value = params.get(name);
  }
  for (const name of ['bag', 'allow_night', 'roundtrip']) {
    if (params.has(name)) form.elements[name].checked = params.get(name) === 'true';
  }
  if (params.has('modes')) {
    const wanted = new Set(params.get('modes').split(','));
    form.querySelectorAll('input[name="mode"]').forEach((box) => { box.checked = wanted.has(box.value); });
  }
  for (const name of ['origin2', 'destination2', 'date2']) {
    const visible = params.has(`show_${name}`) || Boolean(params.get(name));
    document.getElementById(`${name}-field`).hidden = !visible;
    const button = document.querySelector(`[data-toggle="${name}"]`);
    if (button) button.textContent = TOGGLES[name][visible ? 1 : 0];
  }
  returnField.hidden = !form.elements.roundtrip.checked;
  if (params.has('sort')) lastSort = params.get('sort');
  markActivePreset();
  syncReturnMin();

  // Le fermate scelte si applicano dopo il caricamento dei chip, altrimenti
  // non c'e' ancora niente da accendere.
  return Promise.all(PLACE_FIELDS.map(async (field) => {
    await loadStops(field);
    const raw = params.get(`stops_${field}`);
    if (!raw) return;
    const wanted = new Set(raw.split(','));
    const state = stops.get(field);
    state.chosen = new Set(state.nodes.filter((n) => wanted.has(n.id)).map((n) => n.id));
    renderStops(field);
  }));
}

/* ------------------------------------------------------------ profilo --- */

const savedList = document.getElementById('saved-list');
const prefsState = document.getElementById('prefs-state');
const discountList = document.getElementById('discount-list');

let discountsCache = [];

const SCOPE_LABEL = {
  all: 'tutti gli operatori',
  'mode:rail': 'tutti i treni',
  'mode:bus': 'tutti i pullman',
  'mode:air': 'tutti i voli',
  'mode:ferry': 'tutte le navi',
  'mode:transfer': 'i trasferimenti a terra',
};

/** Nome leggibile di ogni ambito, riempito insieme al menu. */
const scopeNames = new Map(Object.entries(SCOPE_LABEL));

/** Le voci del menu "vale su": i modi, piu' un operatore per riga. */
async function fillScopes() {
  const select = document.getElementById('d-scope');
  const fisse = Object.entries(SCOPE_LABEL)
    .map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
  let operatori = '';
  try {
    const response = await fetch('/api/providers');
    if (response.ok) {
      const elenco = await response.json();
      operatori = (elenco.providers || elenco || []).map((p) => {
        const nome = p.name || p.id;
        scopeNames.set(`provider:${p.id}`, nome);
        return `<option value="provider:${escapeHtml(p.id)}">solo ${escapeHtml(nome)}</option>`;
      }).join('');
    }
  } catch { /* senza elenco restano i modi, che coprono il caso comune */ }
  select.innerHTML = fisse + operatori;
  if (discountsCache.length) renderDiscounts(discountsCache);
}

function describeDiscount(d) {
  const quanto = d.kind === 'free'
    ? 'gratis'
    : (d.kind === 'percent' ? `−${d.value}%` : `−${fmtMoney(d.value)} a corsa`);
  const dove = scopeNames.get(d.scope) || d.scope.replace('provider:', '');
  const extra = [];
  if (d.routes && d.routes.length) extra.push(`solo ${d.routes.join(', ')}`);
  if (d.weekdays && d.weekdays.length) extra.push(`solo alcuni giorni`);
  if (d.valid_to) extra.push(`fino al ${fmtDate(d.valid_to)}`);
  return `${quanto} su ${dove}${extra.length ? ' · ' + extra.join(' · ') : ''}`;
}

function renderDiscounts(elenco) {
  if (!elenco.length) {
    discountList.innerHTML = '<li class="hint">Nessuna tessera. Aggiungine una qui sotto.</li>';
    return;
  }
  discountList.innerHTML = elenco.map((d) => (
    `<li${d.active ? '' : ' class="spenta"'}>`
    + `<label class="inline"><input type="checkbox" data-toggle-discount="${d.id}"${d.active ? ' checked' : ''}>`
    + `<strong>${escapeHtml(d.name)}</strong></label>`
    + ` <span class="hint">${escapeHtml(describeDiscount(d))}</span>`
    + ` <button type="button" class="link" data-delete-discount="${d.id}" title="Elimina">×</button></li>`
  )).join('');
}

discountList.addEventListener('click', async (event) => {
  const remove = event.target.closest('[data-delete-discount]');
  if (remove) {
    await fetch(`/api/profile/discounts/${remove.dataset.deleteDiscount}`, { method: 'DELETE' });
    loadProfile(false);
  }
});

discountList.addEventListener('change', async (event) => {
  const box = event.target.closest('[data-toggle-discount]');
  if (!box) return;
  const id = Number(box.dataset.toggleDiscount);
  const corrente = (discountsCache.find((d) => d.id === id) || {});
  await fetch('/api/profile/discounts', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ...corrente, active: box.checked }),
  });
  loadProfile(false);
});

document.getElementById('d-add').addEventListener('click', async () => {
  const nome = document.getElementById('d-name').value.trim();
  if (!nome) { document.getElementById('d-name').focus(); return; }
  const tratte = document.getElementById('d-routes').value.trim();
  const body = {
    name: nome,
    scope: document.getElementById('d-scope').value,
    kind: document.getElementById('d-kind').value,
    value: Number(document.getElementById('d-value').value) || 0,
    routes: tratte ? tratte.split(',').map((t) => t.trim()).filter(Boolean) : [],
    valid_to: document.getElementById('d-valid-to').value || null,
    active: true,
  };
  await fetch('/api/profile/discounts', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  document.getElementById('d-name').value = '';
  document.getElementById('d-routes').value = '';
  document.getElementById('d-valid-to').value = '';
  loadProfile(false);
});

async function loadProfile(applyPreferences) {
  try {
    const response = await fetch('/api/profile');
    if (!response.ok) return;
    const data = await response.json();
    renderSaved(data.searches || []);
    discountsCache = data.discounts || [];
    renderDiscounts(discountsCache);
    const prefs = data.preferences || {};
    document.getElementById('pref-home').value = prefs.home || '';
    document.getElementById('pref-preset').value = prefs.preset || '';
    if (!applyPreferences) return;
    if (prefs.home && !form.elements.origin.value) {
      form.elements.origin.value = prefs.home;
      loadStops('origin');
    }
    if (prefs.preset) applyPreset(prefs.preset);
    if (prefs.pax) form.elements.pax.value = prefs.pax;
    if (prefs.sort) lastSort = prefs.sort;
    if (Array.isArray(prefs.modes) && prefs.modes.length) {
      const wanted = new Set(prefs.modes);
      form.querySelectorAll('input[name="mode"]').forEach((box) => { box.checked = wanted.has(box.value); });
    }
  } catch { /* il profilo è un di più: senza, l'app funziona identica */ }
}

function renderSaved(searches) {
  if (!searches.length) {
    savedList.innerHTML = '<li class="hint">Nessuna ricerca salvata. Fanne una e premi «Salva questa ricerca».</li>';
    return;
  }
  savedList.innerHTML = searches.map((entry) => (
    `<li data-id="${entry.id}">`
    + `<a href="?${escapeHtml(entry.params)}">${escapeHtml(entry.name)}</a>`
    + (entry.summary ? ` <span class="hint">${escapeHtml(entry.summary)}</span>` : '')
    + ` <button type="button" class="link" data-delete="${entry.id}" title="Elimina">×</button></li>`
  )).join('');
}

savedList.addEventListener('click', async (event) => {
  const remove = event.target.closest('[data-delete]');
  if (!remove) return;
  await fetch(`/api/profile/searches/${remove.dataset.delete}`, { method: 'DELETE' });
  loadProfile(false);
});

document.getElementById('prefs-save').addEventListener('click', async () => {
  const values = {
    home: document.getElementById('pref-home').value.trim(),
    preset: document.getElementById('pref-preset').value,
    modes: [...form.querySelectorAll('input[name="mode"]:checked')].map((b) => b.value),
    pax: Number(form.elements.pax.value) || 1,
    sort: lastSort,
  };
  try {
    await fetch('/api/profile/preferences', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(values),
    });
    prefsState.textContent = 'Preferenze salvate.';
  } catch {
    prefsState.textContent = 'Non sono riuscito a salvarle.';
  }
  setTimeout(() => { prefsState.textContent = ''; }, 4000);
});

saveBtn.addEventListener('click', async () => {
  const params = currentParams().toString();
  const suggerito = panels.length
    ? panels.map((p) => p.title).join(' · ')
    : `${form.elements.origin.value} → ${form.elements.destination.value}`;
  const name = prompt('Nome della ricerca salvata:', suggerito);
  if (!name) return;
  const totale = panels.reduce((sum, panel) => sum + panel.trips.length, 0);
  try {
    await fetch('/api/profile/searches', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        name: name.trim(),
        params,
        summary: totale ? `${totale} soluzioni trovate il ${fmtDate(new Date().toISOString())}` : null,
      }),
    });
    loadProfile(false);
    document.getElementById('profile').open = true;
  } catch { /* niente da fare: il link nell'indirizzo resta comunque valido */ }
});

/* ------------------------------------------------------------- ricerca --- */

form.addEventListener('submit', (event) => {
  event.preventDefault();
  startSearch();
});

stopBtn.addEventListener('click', () => {
  panels.forEach((panel) => panel.stop('Interrotta'));
  refreshSummary();
});

const confirmBox = document.getElementById('confirm-many');
document.getElementById('confirm-cancel').addEventListener('click', () => { confirmBox.hidden = true; });
document.getElementById('confirm-go').addEventListener('click', () => {
  confirmBox.hidden = true;
  startSearch(true);
});

/** I valori scritti in un campo e nel suo gemello, se aperto. */
function entries(primary, secondary) {
  const out = [{ field: primary, value: form.elements[primary].value.trim() }];
  const twin = document.getElementById(`${secondary}-field`);
  if (twin && !twin.hidden) {
    const value = form.elements[secondary].value.trim();
    if (value) out.push({ field: secondary, value });
  }
  return out.filter((entry) => entry.value);
}

/** Ogni partenza con ogni meta con ogni data, meno le combinazioni assurde. */
function buildCombos() {
  const origins = entries('origin', 'origin2');
  const destinations = entries('destination', 'destination2');
  const dates = entries('date', 'date2');
  if (!origins.length || !destinations.length || !dates.length) {
    say('Servono partenza, meta e data.');
    return null;
  }
  const combos = [];
  for (const origin of origins) {
    for (const destination of destinations) {
      if (origin.value.trim().toLowerCase() === destination.value.trim().toLowerCase()) continue;
      for (const date of dates) combos.push({ origin, destination, date: date.value });
    }
  }
  if (!combos.length) {
    say('Partenza e meta coincidono: cambiane una.');
    return null;
  }
  return combos;
}

function chosenNodes(field) {
  return [...stops.get(field).chosen].join(',');
}

function buildUrl(combo, { reverse = false } = {}) {
  const data = new FormData(form);
  const modes = data.getAll('mode');
  const origin = reverse ? combo.destination : combo.origin;
  const destination = reverse ? combo.origin : combo.destination;
  const params = new URLSearchParams({
    origin: origin.value,
    destination: destination.value,
    date: reverse ? form.elements.return_date.value : combo.date,
    pax: data.get('pax') || '1',
    modes: modes.join(','),
    bag: form.elements.bag.checked,
    allow_night: form.elements.allow_night.checked,
    max_changes: data.get('max_changes') || '3',
  });
  if (data.get('budget')) params.set('budget', data.get('budget'));
  // I due orari valgono per l'andata: al ritorno non significano niente.
  if (data.get('depart_after') && !reverse) params.set('depart_after', data.get('depart_after'));
  if (data.get('arrive_by') && !reverse) params.set('arrive_by', data.get('arrive_by'));
  for (const key of WEIGHT_KEYS) params.set(key, data.get(key));
  const from = chosenNodes(origin.field);
  const to = chosenNodes(destination.field);
  if (from) params.set('origin_nodes', from);
  if (to) params.set('destination_nodes', to);
  return `/api/search?${params}`;
}

function startSearch(confirmed = false) {
  say('');
  if (!form.querySelectorAll('input[name="mode"]:checked').length) {
    say('Seleziona almeno un mezzo di trasporto.');
    return;
  }
  const combos = buildCombos();
  if (!combos) return;

  const roundtrip = form.elements.roundtrip.checked;
  if (roundtrip && !form.elements.return_date.value) {
    say('Manca la data del ritorno.');
    return;
  }
  if (roundtrip && combos.some((c) => form.elements.return_date.value < c.date)) {
    say('Il ritorno è prima dell’andata.');
    return;
  }

  const ricerche = combos.length * (roundtrip ? 2 : 1);
  if (ricerche > 2 && !confirmed) {
    // A cache fredda una tratta costa una quarantina di secondi, e il collo di
    // bottiglia e' il limite di una richiesta al secondo per dominio: le
    // ricerche in piu' si sommano, non si sovrappongono.
    const minuti = Math.max(1, Math.round((ricerche * 40) / 60));
    document.getElementById('confirm-text').textContent =
      `Stai per lanciare ${ricerche} ricerche (${combos.length} combinazion${combos.length === 1 ? 'e' : 'i'}`
      + `${roundtrip ? ', andata e ritorno' : ''}). Possono volerci ${minuti} minut${minuti === 1 ? 'o' : 'i'}`
      + ` se i risultati non sono già in cache.`;
    confirmBox.hidden = false;
    return;
  }

  panels.forEach((panel) => panel.close());
  panels.length = 0;
  resultsEl.replaceChildren();
  compareEl.hidden = true;
  compareEl.innerHTML = '';

  statusBox.hidden = false;
  stopBtn.hidden = false;
  goBtn.disabled = true;
  phaseEl.textContent = combos.length > 1 ? `${combos.length} ricerche avviate` : 'Ricerca avviata';
  countsEl.textContent = '';

  const piuDate = new Set(combos.map((c) => c.date)).size > 1;
  combos.forEach((combo) => {
    const panel = new SearchPanel(combo, {
      roundtrip,
      returnDate: roundtrip ? form.elements.return_date.value : null,
      showDate: piuDate,
    });
    panels.push(panel);
    panel.start(buildUrl(combo), roundtrip ? buildUrl(combo, { reverse: true }) : null);
  });
  relayout();

  history.replaceState(null, '', `?${currentParams()}`);
}

/**
 * Assegna a ogni colonna visibile la sua posizione nella griglia.
 *
 * Le colonne non sono scatole indipendenti ma celle di un'unica griglia (vedi
 * `main.multi` nel CSS), quindi riga e colonna vanno ricalcolate ogni volta che
 * una si chiude o si riapre — altrimenti resterebbe il buco dove stava.
 */
function relayout() {
  const columns = panels.length > 1 ? MAX_COLUMNS : 1;
  resultsEl.classList.toggle('multi', panels.length > 1);
  resultsEl.style.setProperty('--panels', String(columns));
  panels.forEach((panel, index) => {
    panel.root.style.setProperty('--col', String((index % columns) + 1));
    panel.root.style.setProperty('--rowbase', String(Math.floor(index / columns) * PANEL_ROWS));
  });
}

document.getElementById('reset-search').addEventListener('click', resetSearch);

/** Riporta tutto com'era all'apertura, risultati compresi. */
function resetSearch() {
  panels.forEach((panel) => panel.close());
  panels.length = 0;
  resultsEl.replaceChildren();
  resultsEl.classList.remove('multi');
  compareEl.hidden = true;
  compareEl.innerHTML = '';
  statusBox.hidden = true;
  confirmBox.hidden = true;
  say('');
  nlFeedback.hidden = true;

  form.reset();
  form.elements.date.value = oggiLocale();
  for (const name of ['origin2', 'destination2', 'date2']) {
    document.getElementById(`${name}-field`).hidden = true;
    const button = document.querySelector(`[data-toggle="${name}"]`);
    if (button) button.textContent = TOGGLES[name][0];
  }
  returnField.hidden = true;
  // `form.reset()` rimette i valori scritti nell'HTML ma non riallinea gli
  // <output> dei cursori, che li seguono via evento.
  document.querySelectorAll('#weights input[type="range"]').forEach((slider) => {
    slider.dispatchEvent(new Event('input', { bubbles: true }));
  });
  for (const field of PLACE_FIELDS) {
    const state = stops.get(field);
    state.query = '';
    state.nodes = [];
    state.chosen.clear();
    renderStops(field);
  }
  lastSort = 'score';
  goBtn.disabled = false;
  history.replaceState(null, '', location.pathname);
  form.elements.origin.focus();
}

/** La riga in cima riassume: le fasi per combinazione stanno nei pannelli. */
function refreshSummary() {
  const running = panels.filter((panel) => panel.running);
  const total = panels.reduce((sum, panel) => sum + panel.trips.length, 0);
  countsEl.textContent = total ? `${total} soluzioni in tutto` : '';
  if (running.length) {
    phaseEl.textContent = panels.length > 1
      ? `${running.length} di ${panels.length} ricerche in corso`
      : 'Ricerca in corso';
    return;
  }
  stopBtn.hidden = true;
  goBtn.disabled = false;
  const interrotte = panels.length && panels.every((panel) => panel.endLabel === 'Interrotta');
  phaseEl.textContent = interrotte
    ? 'Interrotta'
    : (total ? (panels.length > 1 ? 'Ricerche completate' : 'Ricerca completata') : 'Nessuna soluzione trovata');
  if (!interrotte) askCompare();
}

/* ------------------------------------------- consiglio che mette a confronto */

// Con due o piu' combinazioni i consigli per colonna non servono: ciascuno vede
// solo la propria classifica e nessuno puo' dire "vai a Palermo, costa 30 euro
// meno". Si chiede quindi un consiglio solo, che li confronta.
async function askCompare() {
  const utili = panels.filter((panel) => panel.trips.length);
  if (utili.length < 2) return;
  compareEl.hidden = false;
  compareEl.innerHTML = '<h2>Consiglio</h2><p class="hint">Sto confrontando le possibilità…</p>';

  const body = {
    pax: Number(form.elements.pax.value) || 1,
    with_checked_bag: form.elements.bag.checked,
    max_budget: Number(form.elements.budget.value) || null,
    raw_text: form.elements.nl.value.trim() || null,
    candidates: utili.map((panel) => ({
      label: panel.title,
      origin: panel.combo.origin.value,
      destination: panel.combo.destination.value,
      date: panel.combo.date,
      return_date: panel.options.returnDate,
      options: panel.trips.slice(0, 5).map((trip) => ({
        depart: trip.depart,
        arrive: trip.out.arrive,
        duration_min: trip.duration_min,
        total: Number(trip.total.toFixed(2)),
        modes: trip.modes,
        operators: trip.operators,
        n_changes: trip.n_changes,
        n_tickets: trip.n_tickets,
        flags: trip.flags,
        return_depart: trip.back ? trip.back.depart : null,
        return_arrive: trip.back ? trip.back.arrive : null,
      })),
    })),
  };

  try {
    const response = await fetch('/api/advice/compare', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = response.ok ? await response.json() : {};
    if (!data.text) { compareEl.hidden = true; compareEl.innerHTML = ''; return; }
    compareEl.innerHTML = '<h2>Consiglio</h2>'
      + data.text.split('\n').filter(Boolean).map((p) => `<p>${escapeHtml(p)}</p>`).join('');
  } catch {
    compareEl.hidden = true;
    compareEl.innerHTML = '';
  }
}

/* ------------------------------------------------ coppie andata/ritorno --- */

/** Quante combinazioni tenere, e quante volte al massimo la stessa andata. */
const MAX_TRIPS = 25;
const MAX_PER_OUTBOUND = 3;

function oneWayTrip(itinerary) {
  return {
    id: itinerary.id,
    out: itinerary,
    back: null,
    total: itinerary.cost.total,
    duration_min: itinerary.duration_min,
    depart: itinerary.depart,
    arrive: itinerary.arrive,
    n_changes: itinerary.n_changes,
    n_tickets: itinerary.n_tickets,
    score: itinerary.score,
    flags: itinerary.flags || [],
    modes: itinerary.modes || [],
    operators: itinerary.operators || [],
    is_new: itinerary.is_new,
  };
}

function pairTrip(out, back) {
  return {
    id: `${out.id}|${back.id}`,
    out,
    back,
    total: out.cost.total + back.cost.total,
    duration_min: out.duration_min + back.duration_min,
    depart: out.depart,
    arrive: back.arrive,
    n_changes: out.n_changes + back.n_changes,
    n_tickets: out.n_tickets + back.n_tickets,
    score: out.score + back.score,
    flags: [...new Set([...(out.flags || []), ...(back.flags || [])])],
    modes: [...new Set([...(out.modes || []), ...(back.modes || [])])],
    operators: [...new Set([...(out.operators || []), ...(back.operators || [])])],
    is_new: out.is_new || back.is_new,
  };
}

/**
 * Le combinazioni andata+ritorno possibili, classificate per punteggio.
 *
 * Venticinque andate per venticinque ritorni fanno seicentoventicinque coppie,
 * e le migliori sarebbero venticinque varianti dello stesso volo: il vincolo
 * di diversita' impedisce che una sola andata occupi tutta la classifica.
 */
function buildTrips(outbound, inbound) {
  const pairs = [];
  for (const out of outbound) {
    const arrivo = new Date(out.arrive);
    for (const back of inbound) {
      if (new Date(back.depart) <= arrivo) continue;  // non si torna prima di arrivare
      pairs.push(pairTrip(out, back));
    }
  }
  pairs.sort((a, b) => b.score - a.score);

  const quante = new Map();
  const kept = [];
  for (const pair of pairs) {
    const usate = quante.get(pair.out.id) || 0;
    if (usate >= MAX_PER_OUTBOUND) continue;
    quante.set(pair.out.id, usate + 1);
    kept.push(pair);
    if (kept.length >= MAX_TRIPS) break;
  }
  return kept;
}

/* --------------------------------------------------------- un pannello --- */

/** Quante righe della griglia occupa un pannello: serve ad allineare le colonne. */
const PANEL_ROWS = 8;

class SearchPanel {
  constructor(combo, options) {
    this.combo = combo;
    this.options = options;
    this.streams = { out: null, back: null };
    this.endLabel = '';
    this.outbound = [];
    this.inbound = [];
    this.trips = [];
    this.phaseText = 'In coda…';
    this.countText = '';
    this.sort = lastSort;
    this.modeFilter = '';
    /** Le schede aperte restano aperte quando la classifica si aggiorna. */
    this.expanded = new Set();
    this.providerState = new Map();
    this.activeProviders = new Set();

    this.title = `${combo.origin.value} → ${combo.destination.value}`;
    if (options.showDate) this.title += ` · ${fmtDate(combo.date)}`;

    const node = panelTemplate.content.firstElementChild.cloneNode(true);
    this.root = node;
    this.routeEl = node.querySelector('.route');
    this.metaEl = node.querySelector('.panel-counts');
    this.sortEl = node.querySelector('.sort-by');
    this.modeEl = node.querySelector('.mode-filter');
    this.planEl = node.querySelector('.plan');
    this.deepEl = node.querySelector('.deep');
    this.providersEl = node.querySelector('.providers');
    this.filterEl = node.querySelector('.filter-line');
    this.knownEl = node.querySelector('.known-routes');
    this.adviceEl = node.querySelector('.advice');
    this.cardsEl = node.querySelector('.cards');

    this.routeEl.textContent = this.title;
    this.sortEl.value = this.sort;
    this.sortEl.title = 'Riordina le soluzioni già trovate (le prime 25 per punteggio).';
    this.sortEl.addEventListener('change', () => {
      this.sort = this.sortEl.value;
      lastSort = this.sort;
      this.render();
    });
    this.modeEl.addEventListener('change', () => {
      this.modeFilter = this.modeEl.value;
      this.render();
    });
    const toggle = node.querySelector('.toggle-panel');
    toggle.addEventListener('click', () => {
      // Si chiude quello che non interessa, non si butta: la testata resta,
      // con la sua rotta e i suoi conteggi, e un altro click riapre. Rifare la
      // ricerca costerebbe minuti.
      const aperto = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!aperto));
      toggle.textContent = aperto ? '▸' : '▾';
      this.collapse(aperto);
    });
    this.providersEl.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip[data-provider]');
      if (!chip || chip.classList.contains('mute')) return;
      const id = chip.dataset.provider;
      if (this.activeProviders.has(id)) this.activeProviders.delete(id);
      else this.activeProviders.add(id);
      this.renderProviders();
      this.render();
    });
    this.filterEl.addEventListener('click', (event) => {
      if (!event.target.closest('[data-clear]')) return;
      event.preventDefault();
      this.activeProviders.clear();
      this.modeFilter = '';
      this.modeEl.value = '';
      this.renderProviders();
      this.render();
    });
    this.cardsEl.innerHTML = '<p class="empty-state">Sto interrogando gli operatori…</p>';
    resultsEl.appendChild(node);
    this.syncMeta();
  }

  get running() {
    return Boolean(this.streams.out || this.streams.back);
  }

  /** Chiude o riapre il contenuto, lasciando la testata al suo posto. */
  collapse(chiudi) {
    this.chiuso = chiudi;
    for (const figlio of this.root.children) {
      if (figlio.classList.contains('panel-head')) continue;
      // I blocchi che hanno un `hidden` proprio (la barra della ricerca
      // approfondita, gli operatori non interrogabili, il filtro attivo) non
      // devono riaprirsi solo perche' si riapre la colonna: si ricorda com'era
      // e si rimette com'era.
      if (chiudi) {
        figlio.dataset.eraNascosto = String(figlio.hidden);
        figlio.hidden = true;
      } else {
        figlio.hidden = figlio.dataset.eraNascosto === 'true';
      }
    }
  }

  syncMeta() {
    // Il conteggio si ricalcola qui e non dove arrivano i risultati: con
    // andata e ritorno l'ultimo aggiornamento puo' arrivare mentre l'altro
    // stream e' ancora aperto, e senza questo il "finora..." restava scritto
    // anche a ricerca finita.
    if (this.trips.length || !this.running) {
      const cosa = this.options.roundtrip ? 'combinazioni' : 'soluzioni';
      this.countText = `${this.trips.length} ${cosa}${this.running ? ' finora…' : ''}`;
    }
    this.metaEl.textContent = [this.phaseText, this.countText].filter(Boolean).join(' · ');
  }

  start(outUrl, backUrl) {
    this.streams.out = this.open(outUrl, 'out');
    if (backUrl) this.streams.back = this.open(backUrl, 'back');
    this.phaseText = 'Avviata';
    this.syncMeta();
  }

  open(url, which) {
    const stream = new EventSource(url);
    const parse = (handler) => (event) => handler.call(this, JSON.parse(event.data), which);
    stream.addEventListener('resolved', parse(this.onResolved));
    stream.addEventListener('plan', parse(this.onPlan));
    stream.addEventListener('phase', parse(this.onPhase));
    stream.addEventListener('known_routes', parse(this.onKnownRoutes));
    stream.addEventListener('provider', parse(this.onProvider));
    stream.addEventListener('itineraries', parse(this.onItineraries));
    stream.addEventListener('advice', parse(this.onAdvice));
    stream.addEventListener('error', (event) => {
      if (!event.data) return;
      const payload = JSON.parse(event.data);
      this.cardsEl.innerHTML = `<p class="empty-state">${escapeHtml(payload.message)}</p>`;
    });
    stream.addEventListener('done', () => this.closeOne(which));
    stream.onerror = () => {
      // EventSource riprova da solo all'infinito: qui non serve, la ricerca è
      // una richiesta unica e riavviarla raddoppierebbe il carico sugli
      // operatori — con piu' combinazioni lo moltiplicherebbe.
      if (stream.readyState === EventSource.CLOSED) this.closeOne(which);
    };
    return stream;
  }

  closeOne(which) {
    const stream = this.streams[which];
    if (!stream) return;
    stream.close();
    this.streams[which] = null;
    if (this.running) {
      this.phaseText = which === 'out' ? 'Andata pronta, cerco il ritorno…' : 'Ritorno pronto…';
      this.syncMeta();
      return;
    }
    this.endLabel = this.trips.length ? 'Completata' : 'Nessuna soluzione';
    this.phaseText = this.endLabel;
    this.syncMeta();
    refreshSummary();
  }

  /** Chiude tutto senza toccare il riepilogo (serve al riavvio). */
  close() {
    for (const which of ['out', 'back']) {
      if (this.streams[which]) { this.streams[which].close(); this.streams[which] = null; }
    }
  }

  stop(label) {
    if (!this.running) return;
    this.close();
    this.endLabel = label;
    this.phaseText = label;
    this.syncMeta();
  }

  onResolved(data, which) {
    if (which !== 'out') return;
    const fmt = (place) => `${place.label}: ${place.nodes.length} fermate`;
    this.planEl.textContent = `${fmt(data.origin)} · ${fmt(data.destination)}`;
  }

  onPlan(data, which) {
    if (which !== 'out') return;
    const routes = data.paths.map((p) => p.reason).join(' · ');
    this.planEl.innerHTML = `${escapeHtml(this.planEl.textContent)}<br>${data.paths.length} percorsi da provare `
      + `(${escapeHtml(routes)}) — ${data.requests} interrogazioni`;
  }

  onPhase(data, which) {
    if (data.phase === 'veloce') {
      this.phaseText = which === 'back' ? 'Cerco il ritorno…' : 'Operatori rapidi…';
      this.syncMeta();
      this.deepEl.hidden = true;
      return;
    }
    // La fase approfondita apre browser veri: dura molto piu' a lungo e va detto,
    // altrimenti sembra che la ricerca sia finita e poi si muova da sola.
    const who = (data.providers || []).join(', ');
    if (!data.pending) { this.deepEl.hidden = true; return; }
    this.deepEl.hidden = false;
    this.deepEl.innerHTML = `<span class="spinner"></span> Ricerca approfondita in corso`
      + (who ? ` su ${escapeHtml(who)}` : '')
      + ` — i risultati si aggiungono qui sotto.`;
  }

  // Operatori che questa tratta la fanno davvero ma che non sappiamo
  // interrogare. Vanno tenuti fuori dalla classifica, perche' di loro non
  // conosciamo ne' orari ne' prezzi, e vanno detti lo stesso: chi cerca
  // Torino-Roma e non vede Italo puo' credere che non esista.
  onKnownRoutes(operators, which) {
    if (which !== 'out' || !operators || !operators.length) return;
    const items = operators.map((op) => {
      const note = op.note ? ` — ${escapeHtml(op.note)}` : '';
      const link = op.url
        ? `<a href="${escapeHtml(op.url)}" target="_blank" rel="noopener">${escapeHtml(op.name)}</a>`
        : escapeHtml(op.name);
      return `<li>${link}${note}</li>`;
    });
    this.knownEl.innerHTML = '<h2>Anche questi collegano le due città</h2>'
      + '<p>I loro orari non sono interrogabili da qui: vanno controllati sul loro sito.</p>'
      + `<ul>${items.join('')}</ul>`;
    this.knownEl.hidden = false;
  }

  onProvider(report) {
    const previous = this.providerState.get(report.provider);
    // Con andata e ritorno lo stesso operatore riporta due volte: vince chi ha
    // trovato qualcosa, altrimenti un "vuoto" al ritorno cancellerebbe le
    // corse dell'andata dai chip.
    if (!previous || (report.legs_found || 0) >= (previous.legs_found || 0)) {
      this.providerState.set(report.provider, report);
    }
    this.renderProviders();
  }

  renderProviders() {
    const filtering = this.activeProviders.size > 0;
    const chips = [...this.providerState.values()].map((report) => {
      const usable = report.legs_found > 0;
      const on = this.activeProviders.has(report.provider);
      const detail = report.detail ? ` — ${report.detail}` : '';
      const count = report.legs_found ? ` ${report.legs_found}` : '';
      const title = usable
        ? (on ? 'Mostrato: clicca per toglierlo dal filtro' : 'Clicca per vedere solo questo operatore')
        : report.status + detail;
      return `<button type="button" class="chip ${report.status}${usable ? '' : ' mute'}`
        + `${on ? ' on' : ''}${filtering && usable && !on ? ' off' : ''}"`
        + ` data-provider="${escapeHtml(report.provider)}" aria-pressed="${on}"`
        + ` title="${escapeHtml(title)}">${escapeHtml(report.provider)}${count}</button>`;
    });
    this.providersEl.innerHTML = chips.join('');
  }

  onItineraries(data, which) {
    if (which === 'back') this.inbound = data.items || [];
    else this.outbound = data.items || [];

    this.trips = this.options.roundtrip
      ? buildTrips(this.outbound, this.inbound)
      : this.outbound.map(oneWayTrip);

    this.syncMeta();
    this.render();
    refreshSummary();
  }

  onAdvice(data, which) {
    // Con piu' combinazioni il consiglio e' uno solo, in cima, e confronta:
    // due consigli per colonna non potrebbero dirsi niente a vicenda.
    if (which !== 'out' || panels.length > 1 || !data || !data.text) return;
    this.adviceEl.hidden = false;
    this.adviceEl.innerHTML = `<h2>Consiglio${this.options.roundtrip ? ' sull\'andata' : ''}</h2>`
      + data.text.split('\n').filter(Boolean).map((p) => `<p>${escapeHtml(p)}</p>`).join('');
  }

  /* ----------------------------------------------------- resa risultati --- */

  visible() {
    let trips = this.trips;
    if (this.activeProviders.size) {
      const usa = (itinerary) => (itinerary?.legs || []).some(
        (leg) => this.activeProviders.has(leg.provider));
      trips = trips.filter((trip) => usa(trip.out) || usa(trip.back));
    }
    if (this.modeFilter) {
      // "Solo aereo" vuol dire davvero solo aereo: un volo piu' un pullman non
      // e' un viaggio in aereo. I trasferimenti a terra non contano, ci sono
      // per forza in qualunque soluzione.
      trips = trips.filter((trip) => {
        const legs = [...bookableLegs(trip.out), ...bookableLegs(trip.back)];
        return legs.length && legs.every((leg) => leg.mode === this.modeFilter);
      });
    }
    return trips;
  }

  render() {
    const mostrate = this.visible();
    const attivi = [
      ...[...this.activeProviders],
      ...(this.modeFilter ? [MODE_LABEL[this.modeFilter]] : []),
    ];
    this.filterEl.hidden = !attivi.length;
    if (attivi.length) {
      this.filterEl.innerHTML = `Solo ${attivi.map(escapeHtml).join(', ')}: `
        + `${mostrate.length} di ${this.trips.length} — <a href="#" data-clear="1">mostra tutte</a>`;
    }

    if (!mostrate.length) {
      this.cardsEl.innerHTML = this.trips.length
        ? '<p class="empty-state">Nessuna soluzione con questi filtri.</p>'
        : (this.options.roundtrip && this.outbound.length && !this.inbound.length
          ? `<p class="empty-state">Andata trovata (${this.outbound.length} soluzioni). Aspetto il ritorno…</p>`
          : '<p class="empty-state">Ancora niente. Alcuni operatori sono lenti.</p>');
      return;
    }

    // Si ordina una copia: `trips` viene ricostruito a ogni aggiornamento e il
    // punteggio serve comunque per la medaglia e per i pareggi.
    const compare = SORTERS[this.sort] || SORTERS.score;
    const ordered = [...mostrate].sort((a, b) => compare(a, b) || b.score - a.score);
    // La medaglia segue il punteggio, non la posizione: ordinando per prezzo,
    // la prima scheda non e' piu' "la migliore".
    const best = mostrate.reduce((a, b) => (b.score > a.score ? b : a));

    // La classifica si riordina quando arrivano i risultati lenti. Se la pagina
    // saltasse in cima a ogni aggiornamento sarebbe illeggibile: si conserva la
    // posizione di scorrimento e le schede aperte restano aperte.
    const scrollY = window.scrollY;
    const fragment = document.createDocumentFragment();
    ordered.forEach((trip) => fragment.appendChild(this.card(trip, trip.id === best.id)));
    this.cardsEl.replaceChildren(fragment);
    if (Math.abs(window.scrollY - scrollY) > 2) window.scrollTo({ top: scrollY });
  }

  card(trip, isBest) {
    const node = tripTemplate.content.firstElementChild.cloneNode(true);
    if (isBest) node.classList.add('best');
    if (trip.is_new) node.classList.add('fresh');

    const tratte = node.querySelector('.tratte');
    tratte.appendChild(tratta(trip.out, trip.back ? 'andata' : ''));
    if (trip.back) tratte.appendChild(tratta(trip.back, 'ritorno'));

    node.querySelector('.total').textContent = fmtMoney(trip.total);
    node.querySelector('.perhead').textContent = trip.back
      ? 'a persona, andata e ritorno'
      : 'a persona, tutto incluso';

    const badges = trip.flags.map((flag) => {
      const [cls, text] = FLAG_TEXT[flag] || ['', flag];
      return `<span class="flag ${cls}">${escapeHtml(text)}</span>`;
    });
    if (isBest) badges.unshift('<span class="flag info">migliore per i tuoi criteri</span>');
    if (trip.is_new) badges.unshift('<span class="flag fresh">nuovo</span>');
    node.querySelector('.flags').innerHTML = badges.join('');

    const detail = node.querySelector('.detail');
    detail.innerHTML = detailFor(trip);

    const button = node.querySelector('.expand');
    if (this.expanded.has(trip.id)) { detail.hidden = false; button.setAttribute('aria-expanded', 'true'); }
    button.addEventListener('click', () => {
      detail.hidden = !detail.hidden;
      button.setAttribute('aria-expanded', String(!detail.hidden));
      if (detail.hidden) this.expanded.delete(trip.id); else this.expanded.add(trip.id);
    });

    return node;
  }
}

/* --------------------------------------------------------- resa comune --- */

function tratta(itinerary, direzione) {
  const node = trattaTemplate.content.firstElementChild.cloneNode(true);
  const offset = fmtDayOffset(itinerary.depart, itinerary.arrive);
  node.querySelector('.dir').textContent = direzione === 'andata' ? '↗' : (direzione === 'ritorno' ? '↙' : '');
  node.querySelector('.clock').textContent =
    `${fmtClock(itinerary.depart)} → ${fmtClock(itinerary.arrive)}${offset}`;
  node.querySelector('.duration').textContent =
    `${fmtDuration(itinerary.duration_min)} · ${fmtDate(itinerary.depart)}`;

  const chain = bookableLegs(itinerary).map((leg) =>
    `<span class="mode">${MODE_ICON[leg.mode] || ''} ${escapeHtml(leg.operator || leg.provider)}</span>`
  ).join(' → ');
  const changes = itinerary.n_changes === 0
    ? 'diretto'
    : `${itinerary.n_changes} camb${itinerary.n_changes === 1 ? 'io' : 'i'}`;
  node.querySelector('.path').innerHTML = `${chain}<br>${changes}`
    + (itinerary.n_tickets > 1 ? ` · ${itinerary.n_tickets} biglietti` : ' · biglietto unico');

  node.querySelector('.tratta-total').textContent = direzione ? fmtMoney(itinerary.cost.total) : '';
  return node;
}

function detailFor(trip) {
  const parti = [['Andata', trip.out]];
  if (trip.back) parti.push(['Ritorno', trip.back]);
  return parti.map(([titolo, itinerary]) => (
    (trip.back ? `<h3>${titolo} · ${fmtDate(itinerary.depart)}</h3>` : '')
    + `<ol class="legs">${renderLegs(itinerary.legs)}</ol>`
    + renderBreakdown(itinerary)
    + `<div class="scoring">${escapeHtml(renderScore(itinerary))}</div>`
  )).join('')
    + (trip.back
      ? `<p class="pair-total">Andata e ritorno, a persona: <strong>${fmtMoney(trip.total)}</strong></p>`
      : '');
}

function renderLegs(legs) {
  const rows = [];
  legs.forEach((leg, index) => {
    const previous = legs[index - 1];
    if (previous) {
      const wait = Math.round((new Date(leg.depart) - new Date(previous.arrive)) / 60000);
      if (wait >= 5) {
        rows.push(`<li class="transfer"><span class="when"></span>`
          + `<span class="what wait">attesa ${fmtDuration(wait)}</span></li>`);
      }
    }
    const isTransfer = leg.mode === 'transfer' || leg.mode === 'walk';
    const price = leg.fare ? ` · ${fmtMoney(leg.fare.amount)}` : '';
    const vehicle = leg.vehicle ? ` · ${escapeHtml(leg.vehicle)}` : '';
    const notes = (leg.notes || []).map(escapeHtml).join(' · ');
    const segments = (leg.segments || []).length > 1
      ? `<br><span class="segments">${(leg.segments || []).map(escapeHtml).join(' · ')}</span>`
      : '';
    const link = leg.booking_url
      ? ` · <a href="${escapeHtml(leg.booking_url)}" target="_blank" rel="noopener">apri sul sito</a>`
      : '';
    rows.push(
      `<li class="${isTransfer ? 'transfer' : ''}">`
      + `<span class="when">${fmtClock(leg.depart)} – ${fmtClock(leg.arrive)}</span>`
      + `<span class="what"><strong>${MODE_ICON[leg.mode] || ''} `
      + `${escapeHtml(leg.origin.name)} → ${escapeHtml(leg.destination.name)}</strong>`
      + `<small>${escapeHtml(MODE_LABEL[leg.mode] || leg.mode)}`
      + `${leg.operator ? ' · ' + escapeHtml(leg.operator) : ''}${vehicle}${price}${link}`
      + `${segments}${notes ? '<br>' + notes : ''}</small></span></li>`
    );
  });
  return rows.join('');
}

function renderBreakdown(itinerary) {
  const lines = itinerary.cost.lines || [];
  if (!lines.length) return '';
  const body = lines.map((line) =>
    `<tr class="${line.kind === 'discount' ? 'sconto' : ''}"><td>${escapeHtml(line.label)}`
    + `${line.estimated ? ' <span class="est">stimato</span>' : ''}</td>`
    + `<td>${line.amount < 0 ? '−' + fmtMoney(Math.abs(line.amount)) : fmtMoney(line.amount)}</td></tr>`
  ).join('');
  return `<div class="breakdown"><table><tbody>${body}`
    + `<tr class="sum"><td>Totale a persona</td><td>${fmtMoney(itinerary.cost.total)}</td></tr>`
    + `</tbody></table></div>`;
}

function renderScore(itinerary) {
  const parts = Object.entries(itinerary.score_parts || {})
    .map(([key, value]) => `${key} ${value.toFixed(2)}`)
    .join('  ');
  const co2 = itinerary.co2_kg ? `  co₂ ${itinerary.co2_kg} kg` : '';
  return `punteggio ${itinerary.score.toFixed(2)}   ${parts}${co2}`;
}

/* ------------------------------------------------------------- avvio --- */

(async function boot() {
  fillScopes();
  const params = new URLSearchParams(location.search);
  const daIndirizzo = params.has('origin') && params.has('destination');
  await loadProfile(!daIndirizzo);
  if (daIndirizzo) {
    await applyParams(params);
    startSearch(true);
  }
})();
