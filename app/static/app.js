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

/**
 * Cresce a ogni ricerca nuova. Le richieste di consiglio partono a ricerca
 * conclusa e tornano quando vogliono: senza un numero di generazione da
 * confrontare al ritorno, quella della ricerca precedente si scriveva sopra i
 * risultati nuovi. La finestra non è stretta — «Cerca» torna cliccabile prima
 * che la richiesta parta.
 */
let generazione = 0;

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

/* -------------------------------------------------------------- tab --- */

// La vista attiva sta nell'hash e non nella querystring, che qui è già occupata
// dai parametri di ricerca: una ricerca salvata è un link `?origin=…`, e
// aggiungerci uno stato di navigazione lo renderebbe ambiguo.
const VISTE = ['ricerca', 'profilo', 'impostazioni', 'info'];

function mostraVista(nome) {
  const scelta = VISTE.includes(nome) ? nome : 'ricerca';
  document.querySelectorAll('.view').forEach((sezione) => {
    sezione.classList.toggle('is-active', sezione.id === `view-${scelta}`);
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    const attiva = tab.dataset.view === scelta;
    tab.classList.toggle('is-active', attiva);
    tab.setAttribute('aria-current', attiva ? 'page' : 'false');
  });
  // Le impostazioni si leggono dal server: caricarle all'avvio significherebbe
  // interrogare i fornitori per una scheda che magari nessuno apre.
  if (scelta === 'impostazioni') caricaImpostazioni();
  return scelta;
}

function vaiA(nome) {
  const scelta = mostraVista(nome);
  if (location.hash.slice(1) !== scelta) {
    history.replaceState(null, '', `${location.pathname}${location.search}#${scelta}`);
  }
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => vaiA(tab.dataset.view));
});
// Il link «Apri le impostazioni» dentro il blocco consiglio è un `#impostazioni`
// normale: qui si traduce in cambio di vista invece che in un salto a un'ancora
// che non esiste.
window.addEventListener('hashchange', () => mostraVista(location.hash.slice(1)));

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
// Le tappe di un viaggio hanno le loro (`stage:<id>`), e nascono quando si
// aggiunge la riga: la mappa cresce invece di avere quattro caselle fisse.
const stops = new Map(PLACE_FIELDS.map((field) => [field, { query: '', nodes: [], chosen: new Set() }]));

function statoFermate(field) {
  let state = stops.get(field);
  if (!state) { state = { query: '', nodes: [], chosen: new Set() }; stops.set(field, state); }
  return state;
}

/** Il testo scritto in quel campo, che stia nel modulo o in una riga tappa. */
function valoreCampo(field) {
  if (field.startsWith('stage:')) {
    const tappa = tappe.find((t) => `stage:${t.id}` === field);
    return tappa ? tappa.destination : '';
  }
  return form.elements[field] ? form.elements[field].value : '';
}

function stopsBox(field) {
  return document.querySelector(`.stops[data-field="${CSS.escape(field)}"]`);
}

async function loadStops(field) {
  const state = statoFermate(field);
  const query = valoreCampo(field).trim();
  if (query === state.query) return;
  state.query = query;
  state.nodes = [];
  state.chosen.clear();
  if (query.length < 2) { renderStops(field); return; }
  try {
    const response = await fetch(`/api/resolve?q=${encodeURIComponent(query)}`);
    if (!response.ok) { renderStops(field); return; }
    const place = await response.json();
    if (valoreCampo(field).trim() !== query) return;  // l'utente ha gia' riscritto
    state.nodes = place.nodes || [];
  } catch { /* senza rete si resta senza chip: la ricerca funziona lo stesso */ }
  renderStops(field);
}

function renderStops(field) {
  const box = stopsBox(field);
  const state = statoFermate(field);
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
    const state = statoFermate(field);
    const id = chip.dataset.node;
    if (state.chosen.has(id)) state.chosen.delete(id); else state.chosen.add(id);
    renderStops(field);
    return;
  }
  const clear = event.target.closest('.stops [data-clear]');
  if (clear) {
    event.preventDefault();
    const field = clear.closest('.stops').dataset.field;
    statoFermate(field).chosen.clear();
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

// La risposta è sempre un viaggio, anche a una tappa sola: la prima tappa
// riempie i campi «Da», «A» e «Quando», le altre diventano righe di tappa.
function applyParsed(q) {
  const prima = (q.stages || [])[0] || {};
  form.elements.origin.value = prima.origin || '';
  form.elements.destination.value = prima.destination || '';
  form.elements.date.value = prima.date || form.elements.date.value;
  form.elements.pax.value = q.pax || 1;
  form.elements.bag.checked = Boolean(q.with_checked_bag);
  form.elements.allow_night.checked = q.allow_night !== false;
  form.elements.budget.value = q.max_budget ?? '';
  form.elements.depart_after.value = prima.depart_after ? String(prima.depart_after).slice(0, 5) : '';
  form.elements.arrive_by.value = prima.arrive_by ? String(prima.arrive_by).slice(0, 5) : '';
  const wanted = new Set(q.modes || []);
  document.querySelectorAll('input[name="mode"]').forEach((box) => {
    box.checked = wanted.size ? wanted.has(box.value) : true;
  });
  // La sosta di una tappa la si legge sulla tappa stessa; la riga che la mostra
  // è quella della tappa dopo, perché è lì che serve saperlo per ripartire.
  tappe.length = 0;
  (q.stages || []).slice(1).forEach((stage, i) => {
    tappe.push({ destination: stage.destination || '', stay: q.stages[i].stay_days || 0 });
  });
  renderStages();
  loadStops('origin');
  loadStops('destination');
}

function describeParsed(q) {
  const stages = q.stages || [];
  const prima = stages[0] || {};
  const percorso = [prima.origin, ...stages.map((s) => s.destination)].filter(Boolean).join(' → ');
  const bits = [percorso, `dal ${fmtDate(prima.date)}`];
  const soste = stages.filter((s) => s.stay_days);
  if (soste.length) {
    bits.push(soste.map((s) => `${s.stay_days} giorn${s.stay_days === 1 ? 'o' : 'i'} a ${s.destination}`).join(', '));
  }
  if (q.pax > 1) bits.push(`${q.pax} persone`);
  if (q.max_budget) bits.push(`max ${q.max_budget} €`);
  if (prima.depart_after) bits.push(`partenza dopo le ${String(prima.depart_after).slice(0, 5)}`);
  if (prima.arrive_by) bits.push(`arrivo entro le ${String(prima.arrive_by).slice(0, 5)}`);
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
  // Le tappe stanno nella querystring come le altre scelte: salvare una ricerca
  // e condividerla restano lo stesso gesto, e un viaggio a tre tappe si riapre
  // da un link come tutto il resto.
  if (tappe.length) {
    params.set('stages', tappe.map((t) => `${t.destination}:${t.stay}`).join('|'));
  }
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
  tappe.length = 0;
  if (params.has('stages')) {
    for (const pezzo of params.get('stages').split('|').filter(Boolean)) {
      const taglio = pezzo.lastIndexOf(':');
      tappe.push({
        destination: taglio > 0 ? pezzo.slice(0, taglio) : pezzo,
        stay: taglio > 0 ? Number(pezzo.slice(taglio + 1)) || 0 : 0,
      });
    }
  }
  renderStages();
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
    const state = statoFermate(field);
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

/* ------------------------------------------------- catalogo delle tessere --- */

// Il catalogo lo genera uno script dai documenti degli operatori, quindi le
// percentuali e le scadenze non sono digitate da nessuno. Qui serve solo a non
// far ricordare a memoria che la Promo Young è il 20% e scade il 30 novembre.
const catalogoSelect = document.getElementById('d-catalogo');
const catalogoNota = document.getElementById('d-catalogo-nota');
let catalogoCache = [];

function etichettaTessera(t) {
  if (t.scaduta) return `${t.nome} — scaduta`;
  if (t.non_ancora_valida) return `${t.nome} — non ancora valida`;
  if (t.acquistabile === false) return `${t.nome} — non più acquistabile`;
  return t.nome;
}

async function caricaCatalogo() {
  try {
    const dati = await (await fetch('/api/tessere')).json();
    catalogoCache = dati.tessere || [];
  } catch { return; }
  // Le utilizzabili prima, le altre dopo: restano in elenco perché chi ha una
  // Carta Verde emessa prima di aprile la sta ancora usando.
  const ordinate = [...catalogoCache].sort(
    (a, b) => Number(a.scaduta || a.acquistabile === false) - Number(b.scaduta || b.acquistabile === false),
  );
  catalogoSelect.innerHTML = '<option value="">— la scrivo a mano —</option>'
    + ordinate.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(etichettaTessera(t))}</option>`).join('');
}

catalogoSelect.addEventListener('change', () => {
  const scelta = catalogoCache.find((t) => t.id === catalogoSelect.value);
  if (!scelta) { catalogoNota.textContent = ''; return; }
  document.getElementById('d-name').value = scelta.nome;
  document.getElementById('d-scope').value = scelta.ambito || 'all';
  document.getElementById('d-kind').value = scelta.tipo || 'percent';
  document.getElementById('d-value').value = scelta.valore ?? 0;
  document.getElementById('d-routes').value = (scelta.rotte || []).join(', ');
  document.getElementById('d-valid-to').value = scelta.valido_a || '';

  const pezzi = [];
  if (scelta.richiede) pezzi.push(`Serve: ${scelta.richiede}.`);
  if (scelta.valido_a) pezzi.push(`Vale fino al ${fmtDate(scelta.valido_a)}.`);
  if (scelta.acquistabile === false) pezzi.push('Non si compra più, ma chi ce l’ha la usa fino alla scadenza.');
  if (scelta.note) pezzi.push(scelta.note);
  if (scelta.estratto) pezzi.push('Letta dalle condizioni di trasporto dell’operatore.');
  else if (scelta.stantia) pezzi.push('Da riverificare: nessuno la guarda da un pezzo.');
  catalogoNota.innerHTML = escapeHtml(pezzi.join(' '))
    + (scelta.fonte ? ` <a href="${scelta.fonte}" target="_blank" rel="noopener">fonte</a>` : '');
});

document.getElementById('d-add').addEventListener('click', async () => {
  const nome = document.getElementById('d-name').value.trim();
  if (!nome) { document.getElementById('d-name').focus(); return; }
  const tratte = document.getElementById('d-routes').value.trim();
  const dalCatalogo = catalogoCache.find((t) => t.id === catalogoSelect.value);
  const body = {
    name: nome,
    scope: document.getElementById('d-scope').value,
    kind: document.getElementById('d-kind').value,
    value: Number(document.getElementById('d-value').value) || 0,
    routes: tratte ? tratte.split(',').map((t) => t.trim()).filter(Boolean) : [],
    valid_to: document.getElementById('d-valid-to').value || null,
    active: true,
    // I nomi delle offerte servono al calcolo: quando l'operatore manda il
    // prezzo ridotto nella risposta, si usa il suo invece di questa percentuale.
    offers: dalCatalogo ? (dalCatalogo.offerte || []) : [],
    catalog_id: dalCatalogo ? dalCatalogo.id : null,
  };
  await fetch('/api/profile/discounts', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  document.getElementById('d-name').value = '';
  document.getElementById('d-routes').value = '';
  document.getElementById('d-valid-to').value = '';
  catalogoSelect.value = '';
  catalogoNota.textContent = '';
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
    // Il profilo non è più un accordion da aprire: è una vista, e mostrarla è
    // il modo di far vedere che la ricerca è finita nell'elenco.
    vaiA('profilo');
  } catch { /* niente da fare: il link nell'indirizzo resta comunque valido */ }
});

/* ------------------------------------------------------- impostazioni --- */

// Etichette, dove si prende la chiave e cosa dà il piano gratuito. Il backend
// sa i nomi dei fornitori ma non ha motivo di sapere dove ci si registra: qui
// c'è la parte che serve a chi deve procurarsi una chiave, e basta.
const CATALOGO_IA = {
  openrouter: {
    label: 'OpenRouter',
    placeholder: 'sk-or-v1-…',
    signup: 'https://openrouter.ai/keys',
    nota: 'Molti modelli :free con una chiave sola. Nessuna carta.',
  },
  groq: {
    label: 'Groq',
    placeholder: 'gsk_…',
    signup: 'https://console.groq.com/keys',
    nota: '30 richieste al minuto, molto veloce. Nessuna carta.',
  },
  cerebras: {
    label: 'Cerebras',
    placeholder: 'csk-…',
    signup: 'https://cloud.cerebras.ai',
    nota: 'Un milione di token al giorno. Nessuna carta.',
  },
  google: {
    label: 'Google AI Studio',
    placeholder: 'AI…',
    signup: 'https://aistudio.google.com/apikey',
    nota: 'Quota gratuita generosa su Gemini Flash. Nessuna carta.',
  },
  mistral: {
    label: 'Mistral',
    placeholder: '…',
    signup: 'https://console.mistral.ai/api-keys',
    nota: 'Piano Experiment gratuito, con limiti di frequenza.',
  },
  local: {
    label: 'Server locale',
    placeholder: 'http://localhost:11434/v1',
    nota: 'Ollama, LM Studio o qualunque endpoint compatibile OpenAI. '
      + 'Qui va l’indirizzo, non una chiave: in locale non c’è nessuno da autenticare.',
  },
  openai: { label: 'OpenAI', placeholder: 'sk-…', signup: 'https://platform.openai.com/api-keys' },
  anthropic: {
    label: 'Anthropic', placeholder: 'sk-ant-…', signup: 'https://console.anthropic.com/settings/keys',
  },
  deepseek: { label: 'DeepSeek', placeholder: 'sk-…', signup: 'https://platform.deepseek.com/api_keys' },
  xai: { label: 'xAI (Grok)', placeholder: 'xai-…', signup: 'https://console.x.ai' },
  glm: { label: 'Zhipu GLM', placeholder: '…', signup: 'https://open.bigmodel.cn' },
};

const freeGrid = document.getElementById('providers-free');
const paidGrid = document.getElementById('providers-paid');
const allowPaid = document.getElementById('allow-paid');
const aiModelsEl = document.getElementById('ai-models');
let impostazioniCaricate = false;

function schedaFornitore(p) {
  const info = CATALOGO_IA[p.name] || { label: p.name, placeholder: '…' };
  const stato = p.configured ? '✓ configurata' : 'nessuna chiave';
  // Solo la nota del catalogo. Quella del backend è un'etichetta, non una nota:
  // su xAI vale «Grok», e accanto al link diventava «Grok ottieni una chiave».
  const note = info.nota || '';
  const spenta = !p.free && !allowPaid.checked;
  const registrati = info.signup
    ? ` <a class="senza-a-capo" href="${info.signup}" target="_blank" rel="noopener">ottieni una chiave</a>`
    : '';
  return `<div class="provider-card ${p.configured ? 'configurata' : ''} ${spenta ? 'spenta' : ''}"
               data-provider="${escapeHtml(p.name)}">
      <div class="provider-head">
        <strong>${escapeHtml(info.label)}</strong>
        <span class="provider-stato">${stato}</span>
      </div>
      ${note || registrati ? `<p class="hint">${escapeHtml(note)}${registrati}</p>` : ''}
      <div class="provider-riga">
        <input type="password" autocomplete="off" spellcheck="false"
               placeholder="${escapeHtml(info.placeholder)}"
               aria-label="Chiave per ${escapeHtml(info.label)}">
        <button type="button" class="salva-chiave">Salva</button>
        ${p.configured ? '<button type="button" class="link togli-chiave">Rimuovi</button>' : ''}
      </div>
    </div>`;
}

function disegnaFornitori(dati) {
  allowPaid.checked = Boolean(dati.allow_paid);
  const perNome = (a, b) => Number(b.configured) - Number(a.configured);
  freeGrid.innerHTML = dati.providers.filter((p) => p.free).sort(perNome).map(schedaFornitore).join('');
  paidGrid.innerHTML = dati.providers.filter((p) => !p.free).sort(perNome).map(schedaFornitore).join('');
}

async function salvaChiavi(valori) {
  const risposta = await fetch('/api/ai/keys', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(valori),
  });
  if (!risposta.ok) throw new Error('salvataggio non riuscito');
  disegnaFornitori(await risposta.json());
  // La chiave nuova cambia l'ordine di prova: mostrarlo subito è il modo di
  // dire che ha davvero preso effetto, senza chiedere di fidarsi.
  caricaModelli();
}

function campoChiave(bottone) {
  const card = bottone.closest('.provider-card');
  return { nome: card.dataset.provider, input: card.querySelector('input') };
}

for (const griglia of [freeGrid, paidGrid]) {
  griglia.addEventListener('click', async (event) => {
    const salva = event.target.closest('.salva-chiave');
    const togli = event.target.closest('.togli-chiave');
    if (!salva && !togli) return;
    const { nome, input } = campoChiave(salva || togli);
    // Il server locale si configura con l'indirizzo, non con una chiave.
    const campo = nome === 'local' ? 'llm_base_url' : `${nome}_api_key`;
    try {
      await salvaChiavi({ [campo]: togli ? '' : input.value });
    } catch {
      input.value = '';
      input.placeholder = 'non salvata, riprova';
    }
  });
}

allowPaid.addEventListener('change', () => {
  salvaChiavi({ allow_paid_providers: allowPaid.checked }).catch(() => {
    allowPaid.checked = !allowPaid.checked;
  });
});

/** Lo stato di un modello in una parola, più il pallino che lo dice a colpo d'occhio. */
function statoModello(m) {
  if (m.penalita > 0) {
    return { classe: 'penalizzato', testo: `penalizzato (−${m.penalita}), scade da sola` };
  }
  if (!m.verificabile) {
    return { classe: 'ignoto', testo: m.gratuito ? 'salute non pubblicata' : 'a pagamento' };
  }
  if (m.vivo === false) return { classe: 'morto', testo: m.detail || 'nessun provider lo serve' };
  const hosts = (m.hosts || []).join(', ');
  return {
    classe: 'vivo',
    testo: [m.uptime_5m != null ? `${m.uptime_5m}% negli ultimi 5 minuti` : '', hosts]
      .filter(Boolean).join(' · '),
  };
}

function opzione(m, scelto) {
  const morto = m.vivo === false ? ' (non disponibile)' : '';
  return `<option value="${escapeHtml(m.id)}"${m.id === scelto ? ' selected' : ''}>`
    + `${escapeHtml(m.id)}${morto}</option>`;
}

function gruppo(etichetta, elenco, scelto) {
  if (!elenco.length) return '';
  return `<optgroup label="${escapeHtml(etichetta)}">`
    + elenco.map((m) => opzione(m, scelto)).join('') + '</optgroup>';
}

function bloccoCompito(chiave, t) {
  const scelto = t.pinned || '';
  const consigliati = t.candidates.filter((m) => m.consigliato);
  const gratuiti = t.candidates.filter((m) => !m.consigliato && m.gratuito);
  const pagamento = t.candidates.filter((m) => !m.gratuito);

  const opzioni = `<option value=""${scelto ? '' : ' selected'}>Automatico`
    + `${t.auto ? ` — adesso ${t.auto}` : ''}</option>`
    + gruppo('Consigliati per questo compito', consigliati, scelto)
    + gruppo('Altri gratuiti', gratuiti, scelto)
    + gruppo(`A pagamento (${pagamento.length})`, pagamento, scelto);

  const riga = (m) => {
    const stato = statoModello(m);
    const attivo = m.id === (scelto || t.auto);
    return `<li class="modello ${stato.classe}${attivo ? ' in-uso' : ''}">`
      + '<span class="pallino" aria-hidden="true"></span>'
      + `<span class="modello-id">${escapeHtml(m.model)}</span>`
      + `<span class="modello-stato">${escapeHtml(stato.testo)}</span>`
      + `${attivo ? '<span class="modello-uso">in uso</span>' : ''}</li>`;
  };

  // In elenco i gratuiti, che sono una dozzina e si leggono. Quelli a pagamento
  // sono centinaia: stanno nel menu, e qui si dice quanti sono invece di
  // troncare in silenzio. Tranne quello scelto, che va visto.
  const mostrati = t.candidates.filter((m) => m.gratuito || m.id === scelto);
  const nascosti = t.candidates.length - mostrati.length;
  const coda = nascosti
    ? `<li class="modelli-coda">e altri ${nascosti} a pagamento, nel menu qui sopra`
      + ' — la loro salute OpenRouter non la pubblica</li>'
    : '';

  return `<section class="compito">
      <div class="compito-head">
        <h4>${escapeHtml(t.label)}</h4>
        <select class="scelta-modello" data-task="${escapeHtml(chiave)}"
                aria-label="Modello per ${escapeHtml(t.label)}">${opzioni}</select>
      </div>
      <ul class="modelli">${mostrati.map(riga).join('') || '<li class="hint">Nessun modello disponibile.</li>'}${coda}</ul>
    </section>`;
}

async function caricaModelli() {
  aiModelsEl.innerHTML = '<p class="hint">Controllo la salute dei modelli…</p>';
  try {
    const dati = await (await fetch('/api/ai/models')).json();
    if (!dati.configured) {
      aiModelsEl.innerHTML = '<p class="hint">Nessuna chiave: l’IA è spenta e il resto del sito funziona identico.</p>';
      return;
    }
    aiModelsEl.innerHTML = Object.entries(dati.tasks)
      .map(([chiave, t]) => bloccoCompito(chiave, t)).join('');
  } catch {
    aiModelsEl.innerHTML = '<p class="hint">Elenco dei modelli non raggiungibile.</p>';
  }
}

aiModelsEl.addEventListener('change', async (event) => {
  const scelta = event.target.closest('.scelta-modello');
  if (!scelta) return;
  scelta.disabled = true;
  try {
    // Stringa vuota = torna in automatico: è la stessa convenzione con cui si
    // toglie una chiave.
    await fetch('/api/ai/keys', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ [`${scelta.dataset.task}_model`]: scelta.value }),
    });
    await caricaModelli();
  } catch {
    scelta.disabled = false;
  }
});

document.getElementById('ai-refresh').addEventListener('click', caricaModelli);

/* ------------------------------------------------------- aggiornamenti --- */

const versionEl = document.getElementById('version-installed');
const updateCheckBtn = document.getElementById('update-check');
const updateInstallBtn = document.getElementById('update-install');
const updateState = document.getElementById('update-state');
/** Serve a riconoscere che a rispondere è il programma nuovo, non il vecchio. */
let versioneInstallata = '';

async function controllaAggiornamenti(automatico = false) {
  if (!automatico) updateState.textContent = 'Controllo in corso…';
  try {
    const dati = await (await fetch('/api/update/check')).json();
    versioneInstallata = dati.installed || '';
    versionEl.textContent = dati.installed || '—';
    updateInstallBtn.hidden = !dati.update_available;
    if (dati.update_available) {
      updateState.textContent = `C'è la ${dati.latest}. `
        + 'Scarico, sostituisco e riapro: i tuoi dati non si toccano.';
    } else if (automatico) {
      updateState.textContent = '';
    } else {
      updateState.textContent = dati.detail || 'Sei già alla versione più recente.';
    }
  } catch {
    if (!automatico) updateState.textContent = 'Non sono riuscito a chiedere a GitHub.';
  }
}

updateCheckBtn.addEventListener('click', () => controllaAggiornamenti());

updateInstallBtn.addEventListener('click', async () => {
  updateInstallBtn.disabled = true;
  updateState.textContent = 'Scarico la versione nuova…';
  try {
    const esito = await (await fetch('/api/update/install', { method: 'POST' })).json();
    if (!esito.started) {
      updateState.textContent = esito.detail || 'Aggiornamento non avviato.';
      updateInstallBtn.disabled = false;
      return;
    }
    // Da qui il server si spegne di proposito: una connessione che cade è il
    // segno che sta andando bene, non che è rotto. Si aspetta che il nuovo
    // risponda e si ricarica la pagina dove si era.
    attendiRiavvio(versioneInstallata);
  } catch {
    updateState.textContent = 'Aggiornamento non avviato.';
    updateInstallBtn.disabled = false;
  }
});

/** Aspetta che risponda la versione NUOVA, non quella che sta morendo.
 *
 *  Il vecchio server resta in piedi per un attimo dopo la risposta, quindi una
 *  domanda del tipo «ci sei?» riceve un sì da lui e la pagina si ricaricherebbe
 *  su un programma che sta chiudendo. Si confronta la versione. */
function attendiRiavvio(prima, scadenza = Date.now() + 300000) {
  const restano = Math.max(0, Math.round((scadenza - Date.now()) / 1000));
  updateState.textContent = 'Sostituzione in corso, il programma si riapre da solo. '
    + `La pagina si ricarica quando è pronto (attendo ancora ${restano} s).`;
  setTimeout(async () => {
    try {
      const risposta = await fetch('/api/health', { cache: 'no-store' });
      if (risposta.ok) {
        const dati = await risposta.json();
        if (dati.version && dati.version !== prima) { location.reload(); return; }
      }
    } catch { /* ancora giù: è quello che ci si aspetta a metà aggiornamento */ }
    if (Date.now() < scadenza) attendiRiavvio(prima, scadenza);
    else {
      updateState.innerHTML = 'Il programma non è tornato su. Guarda '
        + '<code>data/logs/aggiornamento.log</code>: è l’unico posto dove '
        + 'l’aggiornamento lascia traccia.';
    }
  }, 3000);
}

async function caricaImpostazioni() {
  if (impostazioniCaricate) return;
  impostazioniCaricate = true;
  try {
    disegnaFornitori(await (await fetch('/api/ai/keys/status')).json());
    caricaModelli();
    controllaAggiornamenti(true);
  } catch {
    impostazioniCaricate = false;
    freeGrid.innerHTML = '<p class="hint">Impostazioni non raggiungibili.</p>';
  }
}

/* -------------------------------------------------------- viaggio a tappe --- */

// Ogni voce è una meta in più dopo la prima: `stay` sono i giorni passati alla
// meta PRECEDENTE prima di ripartire. La prima tappa è il modulo di ricerca
// stesso — non serve una riga per dire quello che i campi «Da» e «A» già dicono.
const tappe = [];
const stagesList = document.getElementById('stages-list');
const tripSummary = document.getElementById('trip-summary');
const tripLegsEl = document.getElementById('trip-legs');
const tripTotalEl = document.getElementById('trip-total');
const tripTotalNote = document.getElementById('trip-total-note');
const tripAdviceEl = document.getElementById('trip-advice');

/** Il viaggio in corso: `null` quando si sta facendo una ricerca normale. */
let viaggio = null;

function nomeMeta(indice) {
  // La meta di partenza della riga `indice` è la meta della riga precedente,
  // e per la prima è il campo «A».
  return (indice === 0 ? form.elements.destination.value : tappe[indice - 1].destination) || '…';
}

/** Il campo fermate di una tappa. Sull'`id` e non sulla posizione: togliendo
 *  una riga di mezzo gli indici scalano, e le fermate scelte per Torino
 *  finirebbero addosso alla tappa successiva. */
function campoTappa(tappa) {
  return `stage:${tappa.id}`;
}

let prossimoIdTappa = 0;

function renderStages() {
  stagesList.innerHTML = tappe.map((tappa, i) => `
    <div class="stage-row" data-index="${i}">
      <span class="stage-arrow">⤷</span>
      <label class="inline">mi fermo a <strong>${escapeHtml(nomeMeta(i))}</strong> per
        <input type="number" class="stage-stay" min="0" max="60" value="${tappa.stay}"> giorni,
      </label>
      <label class="grow">poi vado a
        <input class="stage-dest" list="places-destination" placeholder="Torino"
               value="${escapeHtml(tappa.destination)}" minlength="2">
      </label>
      <button type="button" class="link stage-remove" title="Togli questa tappa">×</button>
      <div class="stops" data-field="${escapeHtml(campoTappa(tappa))}"></div>
    </div>`).join('');
  // I chip vivono in `stops` e non nell'HTML: ridisegnarli dopo ogni render
  // e' quello che li fa sopravvivere all'aggiunta di una riga.
  for (const tappa of tappe) renderStops(campoTappa(tappa));
}

document.getElementById('add-stage').addEventListener('click', () => {
  if (tappe.length >= 6) return;
  tappe.push({ id: prossimoIdTappa++, destination: '', stay: 1 });
  renderStages();
  stagesList.querySelector('.stage-row:last-child .stage-dest')?.focus();
});

stagesList.addEventListener('input', (event) => {
  const row = event.target.closest('.stage-row');
  if (!row) return;
  const tappa = tappe[Number(row.dataset.index)];
  if (event.target.classList.contains('stage-dest')) tappa.destination = event.target.value;
  if (event.target.classList.contains('stage-stay')) tappa.stay = Number(event.target.value) || 0;
});

// Le fermate si chiedono quando il nome e' finito di scrivere, come per «Da» e
// «A»: a ogni tasto sarebbe una richiesta per lettera.
for (const evento of ['change', 'blur']) {
  stagesList.addEventListener(evento, (event) => {
    if (!event.target.classList.contains('stage-dest')) return;
    const row = event.target.closest('.stage-row');
    const tappa = tappe[Number(row.dataset.index)];
    if (tappa) loadStops(campoTappa(tappa));
  }, true);  // `blur` non risale: si ascolta in discesa
}

stagesList.addEventListener('click', (event) => {
  if (!event.target.closest('.stage-remove')) return;
  const [tolta] = tappe.splice(Number(event.target.closest('.stage-row').dataset.index), 1);
  if (tolta) stops.delete(campoTappa(tolta));
  renderStages();
});

// Cambiare la meta principale cambia il nome scritto nella prima riga: senza
// questo resterebbe «mi fermo a Roma» dopo aver messo Napoli.
form.elements.destination.addEventListener('input', () => { if (tappe.length) renderStages(); });

function giornoDopo(iso, giorni) {
  const quando = new Date(`${iso}T12:00:00`);
  quando.setDate(quando.getDate() + giorni);
  return quando.toISOString().slice(0, 10);
}

/** Le tappe del viaggio, con la prima presa dal modulo.
 *
 *  Ogni tappa porta anche **da dove** prendere le fermate scelte: la meta di
 *  una tappa e la partenza della successiva sono la stessa citta', e quindi lo
 *  stesso insieme di fermate — chi ha detto «a Torino solo Porta Susa» da li'
 *  riparte. */
function costruisciTappe() {
  const prima = {
    origin: form.elements.origin.value.trim(),
    originField: 'origin',
    destination: form.elements.destination.value.trim(),
    destinationField: 'destination',
    date: form.elements.date.value,
    stay: tappe.length ? tappe[0].stay : 0,
  };
  const elenco = [prima];
  tappe.forEach((tappa, i) => {
    elenco.push({
      origin: elenco[i].destination,
      originField: elenco[i].destinationField,
      destination: tappa.destination.trim(),
      destinationField: campoTappa(tappa),
      date: null,               // si scopre quando arriva la tappa prima
      stay: tappe[i + 1] ? tappe[i + 1].stay : 0,
    });
  });
  return elenco;
}

function startTrip() {
  const elenco = costruisciTappe();
  const vuota = elenco.find((tappa) => !tappa.origin || !tappa.destination);
  if (vuota) { say('Manca una meta in una delle tappe.'); return; }
  if (!elenco[0].date) { say('Manca la data di partenza.'); return; }

  generazione += 1;
  panels.forEach((panel) => panel.close());
  panels.length = 0;
  resultsEl.replaceChildren();
  for (const el of [compareEl, tripAdviceEl]) { el.hidden = true; el.innerHTML = ''; }
  tripSummary.hidden = true;

  viaggio = { tappe: elenco, indice: 0 };
  statusBox.hidden = false;
  stopBtn.hidden = false;
  goBtn.disabled = true;
  countsEl.textContent = '';
  history.replaceState(null, '', `?${currentParams()}`);
  avviaTappa();
}

function avviaTappa() {
  const tappa = viaggio.tappe[viaggio.indice];
  phaseEl.textContent = `Tappa ${viaggio.indice + 1} di ${viaggio.tappe.length}: ${tappa.origin} → ${tappa.destination}`;
  // Ogni tappa porta le sue fermate: quelle scelte sotto il suo campo, e in
  // partenza quelle della tappa precedente, che e' la stessa citta'.
  const panel = new SearchPanel(
    { origin: { value: tappa.origin, field: tappa.originField },
      destination: { value: tappa.destination, field: tappa.destinationField },
      date: tappa.date },
    { roundtrip: false, returnDate: null, showDate: true, stageIndex: viaggio.indice },
  );
  panels.push(panel);
  // Un consiglio per tappa sì: è la classifica che l'utente ha davanti. Quello
  // sul viaggio intero arriva dopo, quando le tappe sono tutte chiuse.
  panel.refLetter = String.fromCharCode(65 + viaggio.indice);
  panel.start(buildUrl(panel.combo));
  relayout();
}

/** Avanza alla tappa successiva, o chiude il viaggio. Chiamata da refreshSummary. */
function avanzaViaggio() {
  const finita = panels[viaggio.indice];
  if (!finita || finita.running) return;
  if (finita.endLabel === 'Interrotta') { viaggio.interrotto = true; viaggio = null; return; }

  const migliore = finita.trips[0];
  const prossima = viaggio.tappe[viaggio.indice + 1];
  if (prossima) {
    // La data nasce dall'**arrivo**, non dalla partenza: un notturno arriva il
    // giorno dopo, e ripartire dalla data di partenza farebbe cominciare la
    // tappa successiva prima di essere arrivati.
    const arrivo = migliore ? migliore.out.arrive.slice(0, 10) : viaggio.tappe[viaggio.indice].date;
    prossima.date = giornoDopo(arrivo, viaggio.tappe[viaggio.indice].stay);
    viaggio.indice += 1;
    avviaTappa();
    return;
  }
  chiudiViaggio();
}

function chiudiViaggio() {
  const scelte = viaggio.tappe.map((tappa, i) => ({ tappa, panel: panels[i] }));
  const trovate = scelte.filter(({ panel }) => panel && panel.trips.length);
  const totale = trovate.reduce((somma, { panel }) => somma + panel.trips[0].total, 0);

  tripSummary.hidden = false;
  // Una tappa e la sua sosta sono due voci della stessa lista: il tratto
  // tratteggiato della sosta continua la linea che lega le tappe, e la lista è
  // il percorso.
  tripLegsEl.innerHTML = scelte.map(({ tappa, panel }) => {
    const migliore = panel && panel.trips[0];
    const corpo = migliore
      ? `<div class="trip-leg-when">${escapeHtml(fmtClock(migliore.depart))} → `
        + `${escapeHtml(fmtClock(migliore.out.arrive))} · `
        + `${escapeHtml((migliore.operators || []).join(', '))}</div>`
      : '<div class="trip-leg-vuota">nessuna soluzione trovata</div>';
    const riga = `<li class="trip-leg${migliore ? '' : ' senza-soluzione'}">`
      + '<div class="trip-leg-head">'
      + `<span class="trip-leg-route">${escapeHtml(tappa.origin)} → ${escapeHtml(tappa.destination)}</span>`
      + `<span class="trip-leg-date">${escapeHtml(fmtDate(tappa.date))}</span>`
      + `<span class="trip-leg-price">${migliore ? escapeHtml(fmtMoney(migliore.total)) : '—'}</span>`
      + '</div>' + corpo + '</li>';
    if (!tappa.stay) return riga;
    return riga + `<li class="trip-stay">${tappa.stay} giorn${tappa.stay === 1 ? 'o' : 'i'} `
      + `a ${escapeHtml(tappa.destination)}</li>`;
  }).join('');
  tripTotalEl.textContent = fmtMoney(totale);
  tripTotalNote.textContent = trovate.length < scelte.length
    ? `Manca una tappa su ${scelte.length}: il viaggio non si chiude.`
    : 'A persona, sommando la soluzione migliore di ogni tappa.';

  phaseEl.textContent = 'Viaggio completo';
  chiediConsiglioViaggio(scelte);
  viaggio = null;
}

function chiediConsiglioViaggio(scelte) {
  if (scelte.length < 2) return;
  consiglio(
    tripAdviceEl,
    'Consiglio sul viaggio',
    '/api/advice/trip',
    () => ({
      ...contestoConsiglio(),
      legs: scelte.map(({ tappa, panel }, i) => {
        const migliore = panel && panel.perIA(1)[0];
        return {
          label: `Tappa ${i + 1}`,
          origin: tappa.origin,
          destination: tappa.destination,
          date: tappa.date,
          stay_days: tappa.stay,
          found: panel ? panel.trips.length : 0,
          chosen: migliore || null,
        };
      }),
    }),
    { attesa: 'Sto guardando come stanno insieme le tappe…' },
  );
}

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
  if (!field) return '';
  return [...statoFermate(field).chosen].join(',');
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
  // Un viaggio a tappe non è una ricerca ripetuta: le tappe vanno in sequenza
  // perché la data di una dipende da quando si arriva alla precedente.
  if (tappe.length) { startTrip(); return; }

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

  generazione += 1;
  panels.forEach((panel) => panel.close());
  panels.length = 0;
  resultsEl.replaceChildren();
  for (const el of [compareEl, tripAdviceEl]) { el.hidden = true; el.innerHTML = ''; }
  tripSummary.hidden = true;

  statusBox.hidden = false;
  stopBtn.hidden = false;
  goBtn.disabled = true;
  phaseEl.textContent = combos.length > 1 ? `${combos.length} ricerche avviate` : 'Ricerca avviata';
  countsEl.textContent = '';

  const piuDate = new Set(combos.map((c) => c.date)).size > 1;
  combos.forEach((combo, index) => {
    const panel = new SearchPanel(combo, {
      roundtrip,
      returnDate: roundtrip ? form.elements.return_date.value : null,
      showDate: piuDate,
    });
    // Con più colonne il numero da solo non basta: «3» esiste in ognuna. La
    // lettera dice quale, ed è la stessa che il consiglio scrive fra parentesi.
    if (combos.length > 1) panel.refLetter = String.fromCharCode(65 + index);
    panels.push(panel);
    panel.start(
      buildUrl(combo),
      roundtrip ? buildUrl(combo, { reverse: true }) : null,
    );
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
  generazione += 1;
  panels.forEach((panel) => panel.close());
  panels.length = 0;
  resultsEl.replaceChildren();
  resultsEl.classList.remove('multi');
  for (const el of [compareEl, tripAdviceEl]) { el.hidden = true; el.innerHTML = ''; }
  tripSummary.hidden = true;
  viaggio = null;
  tappe.length = 0;
  renderStages();
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
    const state = statoFermate(field);
    state.query = '';
    state.nodes = [];
    state.chosen.clear();
    renderStops(field);
  }
  // Le fermate delle tappe se ne vanno con le tappe, che qui sono già sparite.
  for (const field of [...stops.keys()]) {
    if (field.startsWith('stage:')) stops.delete(field);
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
    if (!viaggio) {
      phaseEl.textContent = panels.length > 1
        ? `${running.length} di ${panels.length} ricerche in corso`
        : 'Ricerca in corso';
    }
    return;
  }
  // Il viaggio prosegue: la tappa appena chiusa dice quando parte la prossima.
  // `avanzaViaggio` azzera `viaggio` in due casi opposti — il viaggio si è
  // chiuso bene, oppure è stato interrotto — e distinguerli qui è necessario:
  // trattando l'interruzione come una fine si finiva a confrontare le tappe
  // fra loro come se fossero alternative, mentre sono tutte obbligatorie.
  if (viaggio) {
    const eraViaggio = viaggio;
    avanzaViaggio();
    if (viaggio) return;
    if (eraViaggio.interrotto) {
      stopBtn.hidden = true;
      goBtn.disabled = false;
      phaseEl.textContent = 'Viaggio interrotto';
      return;
    }
  }
  stopBtn.hidden = true;
  goBtn.disabled = false;
  const interrotte = panels.length && panels.every((panel) => panel.endLabel === 'Interrotta');
  if (interrotte) { phaseEl.textContent = 'Interrotta'; return; }
  // Con le tappe la riga di stato la scrive `chiudiViaggio`, e il confronto non
  // ha senso: le tappe non sono alternative fra cui scegliere, sono tutte.
  if (tripSummary.hidden) {
    phaseEl.textContent = total
      ? (panels.length > 1 ? 'Ricerche completate' : 'Ricerca completata')
      : 'Nessuna soluzione trovata';
    askCompare();
  }
}

/* ------------------------------------ perché il consiglio a volte non c'è -- */

// Gli stessi motivi che manda il backend (`app/ai/client.py`), detti a chi
// legge. Prima erano tutti lo stesso silenzio, e «manca la chiave» e «il
// fornitore ha rifiutato» si risolvono in due modi diversi.
const PERCHE_NIENTE_IA = {
  not_configured: 'Nessuna chiave IA configurata.',
  no_models: 'Nessun modello disponibile in questo momento.',
  rate_limited: 'Il fornitore ha rifiutato la richiesta (429): troppe in poco tempo.',
  truncated: 'Il modello ha troncato la risposta.',
  garbled: 'Il modello ha risposto col suo ragionamento invece che col consiglio.',
  invented: 'Il modello ha citato soluzioni che non esistono.',
  unanchored: 'Il modello non ha detto di quale soluzione parlava.',
  failed: 'Nessun modello ha risposto.',
};

// Il rimedio, che è un'altra cosa dal motivo. Prima ce n'era uno solo per
// tutti — «le chiavi si mettono nelle impostazioni» — e compariva anche sul
// troncamento, cioè quando le chiavi c'erano e funzionavano: un invito a
// sistemare la cosa sbagliata, e nessun modo di riprovare quella giusta.
const RIMEDIO_IA = {
  not_configured: 'Le chiavi si mettono nelle impostazioni.',
  no_models: 'Scegli un modello nelle impostazioni, o lascia «automatico».',
  rate_limited: 'Riprova fra poco, oppure aggiungi la chiave di un secondo fornitore.',
};
//: Per tutti gli altri: il difetto è del modello, non della configurazione, e
//: riprovando ne tocca un altro — il server lo ha appena penalizzato.
const RIMEDIO_MODELLO = 'Ha sbagliato il modello, non la configurazione: riprovando ne tocca un altro.';

/** Vero se il motivo va scritto a schermo. `nothing` no: significa che non
 *  c'era niente da consigliare, e non è colpa dell'IA. */
function motivoDaDire(motivo) {
  return Boolean(motivo) && motivo !== 'nothing';
}

/** I riferimenti `[3]` / `[B2]` diventano bottoni che portano alla scheda.
 *
 *  Si lavora sul testo **già escapato**: il pattern non contiene niente che
 *  `escapeHtml` possa aver toccato, quindi sostituire dopo è sicuro e non
 *  reintroduce HTML dal modello. Un riferimento che non trova la sua scheda
 *  resta testo semplice: meglio un numero muto di un bottone che non porta da
 *  nessuna parte. */
function conRiferimenti(escapato) {
  return escapato.replace(/\[([A-Za-z]{0,2}\d{1,3})\]/g, (intero, ref) => {
    if (!document.querySelector(`[data-ref="${CSS.escape(ref)}"]`)) return intero;
    return `<button type="button" class="ref-chip" data-goto="${ref}"
      title="Vai a questa soluzione">${ref}</button>`;
  });
}

/** Toglie il markdown che il modello a volte scrive lo stesso.
 *
 *  Il prompt vieta grassetto e titoli, ma un modello che disobbedisce mandava
 *  a schermo `**così**` con gli asterischi in chiaro: la pagina rende testo
 *  semplice, non markdown. Si toglie la marcatura e si tiene la parola. */
function senzaMarcature(testo) {
  return testo
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/(^|\s)\*(\S[^*]*?)\*(?=[\s.,;:!?)]|$)/g, '$1$2')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '');
}

/** Riempie il blocco consiglio: il testo se c'è, altrimenti perché manca.
 *  Il caso «manca» resta smorzato, perché una cornice accesa che dice «non
 *  disponibile» griderebbe più forte del consiglio vero. */
function mostraConsiglio(el, titolo, testo, motivo, riprova) {
  scopri(el);
  el.classList.toggle('advice--muto', !testo);
  if (testo) {
    el.innerHTML = `<h2>${escapeHtml(titolo)}</h2>`
      + senzaMarcature(testo).split('\n').filter(Boolean)
        .map((p) => `<p>${conRiferimenti(escapeHtml(p))}</p>`).join('');
    return;
  }
  const perche = PERCHE_NIENTE_IA[motivo] || PERCHE_NIENTE_IA.failed;
  const rimedio = RIMEDIO_IA[motivo] || RIMEDIO_MODELLO;
  const link = RIMEDIO_IA[motivo] && motivo !== 'rate_limited'
    ? ' <a href="#impostazioni">Apri le impostazioni</a>'
    : '';
  el.innerHTML = `<h2>${escapeHtml(titolo)} non disponibile</h2>`
    + `<p>${escapeHtml(perche)} ${escapeHtml(rimedio)}${link}</p>`
    + (riprova ? '<p><button type="button" class="link riprova-ia">Riprova</button></p>' : '');
  if (riprova) el.querySelector('.riprova-ia').addEventListener('click', riprova);
}

// Il clic su un riferimento porta alla sua scheda e la fa notare per un attimo.
// È il pezzo che rende il consiglio verificabile: leggi «la [4] costa 120,80»,
// clicchi, e hai davanti la scheda con quel prezzo scritto sopra.
document.addEventListener('click', (event) => {
  const chip = event.target.closest('.ref-chip[data-goto]');
  if (!chip) return;
  const scheda = document.querySelector(`[data-ref="${CSS.escape(chip.dataset.goto)}"]`);
  if (!scheda) return;
  scheda.scrollIntoView({ block: 'center', behavior: 'smooth' });
  scheda.classList.remove('puntata');
  // Riavvia l'animazione anche al secondo clic sullo stesso riferimento:
  // togliere la classe non basta se il browser non ha ancora ridisegnato.
  void scheda.offsetWidth;
  scheda.classList.add('puntata');
  setTimeout(() => scheda.classList.remove('puntata'), 2000);
});

/** Mostra un blocco senza scavalcare una colonna chiusa a mano.
 *
 *  `collapse` ricorda com'era ogni figlio per rimetterlo com'era: un blocco
 *  che si accendeva da solo dentro una colonna chiusa veniva ricordato come
 *  «era nascosto», e alla riapertura spariva per sempre. */
function scopri(el) {
  const pannello = el.closest('.panel');
  if (pannello && pannello.querySelector('.toggle-panel').getAttribute('aria-expanded') === 'false') {
    el.dataset.eraNascosto = 'false';
    return;
  }
  el.hidden = false;
}

/* --------------------------------------------------------- i tre consigli --- */

//: Quanto si aspetta prima del tentativo automatico. Il server ha appena
//: penalizzato la coppia (fornitore, modello) che ha fallito, quindi il
//: secondo tentativo non ripete lo stesso errore — ma la penalità va scritta
//: sul database e un attimo di respiro evita anche il throttle dell'host.
const RESPIRO_RIPROVA = 3000;

/** Chiede un consiglio, lo scrive, e se fallisce riprova una volta sola.
 *
 *  Una funzione per tutti e tre i consigli: prima ce n'erano tre, ognuna con
 *  la sua idea di cosa fare quando la risposta non arriva. */
async function consiglio(el, titolo, url, corpo, { attesa, automatico = true } = {}) {
  const gen = generazione;
  const rifai = () => consiglio(el, titolo, url, corpo, { attesa, automatico: true });
  scopri(el);
  el.classList.remove('advice--muto');
  el.innerHTML = `<h2>${escapeHtml(titolo)}</h2><p>${escapeHtml(attesa)}</p>`;

  let dati;
  try {
    const risposta = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(corpo()),
    });
    dati = risposta.ok ? await risposta.json() : { reason: 'failed' };
  } catch {
    // La rete è caduta a metà: anche questo si dice, invece di far sparire il
    // blocco che un attimo prima diceva «sto pensando».
    dati = { reason: 'failed' };
  }
  if (gen !== generazione) return;

  if (dati.text) { mostraConsiglio(el, titolo, dati.text, ''); return; }
  if (!motivoDaDire(dati.reason)) { el.hidden = true; el.innerHTML = ''; return; }

  // Un tentativo automatico, uno solo. Non sui motivi che riprovare non può
  // risolvere: senza chiavi non c'è modello da provare, e insistere su un 429
  // è esattamente il burst che lo ha causato.
  if (automatico && !['not_configured', 'no_models', 'rate_limited'].includes(dati.reason)) {
    el.innerHTML = `<h2>${escapeHtml(titolo)}</h2>`
      + `<p>${escapeHtml(PERCHE_NIENTE_IA[dati.reason] || '')} Riprovo con un altro modello…</p>`;
    await new Promise((ok) => setTimeout(ok, RESPIRO_RIPROVA));
    if (gen !== generazione) return;
    await consiglio(el, titolo, url, corpo, { attesa, automatico: false });
    return;
  }
  mostraConsiglio(el, titolo, null, dati.reason, rifai);
}

/** Il corpo comune a tutte le richieste di consiglio. */
function contestoConsiglio() {
  return {
    pax: Number(form.elements.pax.value) || 1,
    with_checked_bag: form.elements.bag.checked,
    max_budget: Number(form.elements.budget.value) || null,
    raw_text: form.elements.nl.value.trim() || null,
  };
}

/** Una soluzione come la vede il modello: gli stessi numeri della scheda.
 *
 *  `ref` è il numero scritto sulla scheda, ed è tutto il punto: è così che il
 *  consiglio può dire «la [3]» e chi legge sa quale guardare. La durata del
 *  ritorno va a parte — sommarla a quella dell'andata e spedirla con gli orari
 *  della sola andata dava al modello un numero che a schermo non esisteva. */
function opzionePerIA(trip, ref) {
  return {
    ref,
    depart: trip.depart,
    arrive: trip.out.arrive,
    duration_min: trip.out.duration_min,
    total: Number(trip.total.toFixed(2)),
    modes: trip.modes,
    operators: trip.operators,
    n_changes: trip.n_changes,
    n_tickets: trip.n_tickets,
    flags: trip.flags,
    notes: noteDaSapere(trip),
    return_depart: trip.back ? trip.back.depart : null,
    return_arrive: trip.back ? trip.back.arrive : null,
    return_duration_min: trip.back ? trip.back.duration_min : null,
  };
}

/** Quello che la scheda dice nel dettaglio e il totale da solo non dice.
 *
 *  Un prezzo «a partire da» e una tariffa scontata sono condizioni, non
 *  numeri: il modello che vede solo il totale li presenta come definitivi
 *  mentre la scheda, aperta, avverte. */
function noteDaSapere(trip) {
  const note = new Set();
  for (const itinerary of [trip.out, trip.back]) {
    for (const leg of (itinerary && itinerary.legs) || []) {
      for (const nota of leg.notes || []) note.add(nota);
    }
    for (const riga of ((itinerary && itinerary.cost) || {}).lines || []) {
      if (riga.label && riga.label.includes('dichiarato da te')) {
        note.add(`sconto ${riga.label}: non verificato con l'operatore`);
      }
    }
  }
  return [...note].slice(0, 8);
}

/** Il consiglio su una classifica sola: quella che si sta guardando. */
function chiediConsiglio(panel) {
  if (!panel.trips.length) return;
  consiglio(
    // Non più «Consiglio sull'andata»: il titolo diceva a metà una verità che
    // adesso è intera, perché il modello vede la coppia come la vedi tu.
    panel.adviceEl,
    'Consiglio',
    '/api/advice',
    () => ({
      ...contestoConsiglio(),
      label: panel.title,
      origin: panel.combo.origin.value,
      destination: panel.combo.destination.value,
      date: panel.combo.date,
      return_date: panel.options.returnDate,
      depart_after: form.elements.depart_after.value || null,
      arrive_by: form.elements.arrive_by.value || null,
      allow_night: form.elements.allow_night.checked,
      found: panel.trips.length,
      partial: panel.endLabel === 'Interrotta',
      relaxed: panel.relaxed,
      options: panel.perIA(5),
    }),
    { attesa: 'Sto guardando le soluzioni…' },
  );
}

/* ------------------------------------------- consiglio che mette a confronto */

// Con due o piu' combinazioni i consigli per colonna non servono: ciascuno vede
// solo la propria classifica e nessuno puo' dire "vai a Palermo, costa 30 euro
// meno". Si chiede quindi un consiglio solo, che li confronta.
function askCompare() {
  const utili = panels.filter((panel) => panel.trips.length);
  if (utili.length < 2) return;
  consiglio(
    compareEl,
    'Consiglio',
    '/api/advice/compare',
    () => ({
      ...contestoConsiglio(),
      candidates: utili.map((panel) => ({
        label: panel.title,
        origin: panel.combo.origin.value,
        destination: panel.combo.destination.value,
        date: panel.combo.date,
        return_date: panel.options.returnDate,
        found: panel.trips.length,
        partial: panel.endLabel === 'Interrotta',
        relaxed: panel.relaxed,
        options: panel.perIA(5),
      })),
    }),
    { attesa: 'Sto confrontando le possibilità…' },
  );
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

/** Quante righe della griglia occupa un pannello: serve ad allineare le colonne.
 * Va tenuto uguale al numero di righe dichiarate in `main.multi .panel > *`. */
const PANEL_ROWS = 9;

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
    /** I vincoli che il motore ha messo da parte, come arrivano dal server. */
    this.relaxed = [];
    /**
     * Il numero scritto sulla scheda, per `trip.id`. Si assegna una volta
     * sola, a ricerca finita e in ordine di punteggio, e da lì non cambia
     * più: è quello che permette a un consiglio già scritto di continuare a
     * puntare alla soluzione giusta anche dopo che si è riordinato per prezzo
     * o filtrato il mezzo. Numerare la posizione a schermo, invece, farebbe
     * scivolare il consiglio sulla scheda sbagliata al primo riordino.
     */
    this.refs = new Map();
    /** La lettera della colonna: serve solo quando le colonne sono più d'una,
     *  perché lì «3» da solo non dice in quale classifica. */
    this.refLetter = '';
    /** Le schede aperte restano aperte quando la classifica si aggiorna. */
    this.expanded = new Set();
    this.providerState = new Map();
    this.activeProviders = new Set();

    this.title = `${combo.origin.value} → ${combo.destination.value}`;
    if (options.showDate) this.title += ` · ${fmtDate(combo.date)}`;
    if (options.stageIndex !== undefined) this.title = `Tappa ${options.stageIndex + 1} · ${this.title}`;

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
    this.relaxedEl = node.querySelector('.relaxed');
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

  /** Fissa i numeri delle schede. Idempotente: chi ha già il suo lo tiene. */
  numera() {
    const perPunteggio = [...this.trips].sort((a, b) => b.score - a.score);
    for (const trip of perPunteggio) {
      if (!this.refs.has(trip.id)) {
        this.refs.set(trip.id, `${this.refLetter}${this.refs.size + 1}`);
      }
    }
  }

  /** Le prime `quante` soluzioni per punteggio, come le vede il modello. */
  perIA(quante) {
    return [...this.trips]
      .sort((a, b) => b.score - a.score)
      .slice(0, quante)
      .map((trip) => opzionePerIA(trip, this.refs.get(trip.id) || ''));
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
    // I numeri si fissano qui, non a ogni ondata di risultati: durante la
    // ricerca la classifica si rimescola, e un numero che cambia sotto gli
    // occhi non è un riferimento.
    this.numera();
    this.render();
    this.syncMeta();
    // Il consiglio di questa colonna. Con più colonne parallele parla invece
    // il confronto, che le vede tutte insieme (`askCompare`); le tappe di un
    // viaggio non sono colonne parallele — sono in sequenza, e ognuna ha la
    // sua classifica da commentare.
    if (this.options.stageIndex !== undefined || panels.length === 1) {
      chiediConsiglio(this);
    }
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
    // Anche una classifica interrotta è una classifica: le sue schede vanno
    // numerate, o un confronto che le include (una colonna ferma e una finita)
    // manderebbe al modello soluzioni senza nome.
    this.numera();
    this.render();
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

    if (which === 'out') this.showRelaxed(data.relaxed);
    this.syncMeta();
    this.render();
    refreshSummary();
  }

  /** I vincoli che il motore ha messo da parte perche' nessuna soluzione li
   * rispettava (vedi `_build` in `search_service.py`: preferisce dei risultati
   * fuori vincolo a una pagina vuota). Dirlo non e' una gentilezza: senza questa
   * riga si leggono orari che si era chiesto di escludere, e l'unico indizio
   * resta il consiglio dell'IA, che e' opzionale e non sempre lo nota. */
  showRelaxed(relaxed) {
    // Si tengono anche per il consiglio: il modello leggeva «budget massimo
    // 120 euro» sopra una lista che lo sforava tutta, e non aveva modo di
    // accorgersene.
    this.relaxed = relaxed || [];
    if (!relaxed || !relaxed.length) { this.relaxedEl.hidden = true; return; }
    const detto = {
      depart_after: (v) => `partenza dopo le ${v}`,
      arrive_by: (v) => `arrivo entro le ${v}`,
      max_budget: (v) => `budget massimo ${String(v).replace('.', ',')} €`,
      max_changes: (v) => `non più di ${v} ${Number(v) === 1 ? 'cambio' : 'cambi'}`,
      allow_night: () => 'niente viaggi notturni',
    };
    const voci = relaxed
      .filter((r) => detto[r.kind])
      .map((r) => `«${detto[r.kind](r.value)}»`);
    if (!voci.length) { this.relaxedEl.hidden = true; return; }
    const elenco = voci.length > 1
      ? `${voci.slice(0, -1).join(', ')} e ${voci[voci.length - 1]}`
      : voci[0];
    const quel = voci.length > 1 ? 'quei vincoli' : 'quel vincolo';
    this.relaxedEl.hidden = false;
    this.relaxedEl.textContent = `Nessuna soluzione rispetta ${elenco}: `
      + `qui sotto ci sono le migliori senza ${quel}.`;
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

    // Il numero della scheda. Finché la ricerca gira non c'è: la classifica si
    // rimescola a ogni ondata e un numero ballerino sarebbe peggio di nessun
    // numero. Compare a ricerca finita, insieme al consiglio che lo cita.
    const ref = this.refs.get(trip.id);
    const refEl = node.querySelector('.ref');
    if (ref) {
      node.dataset.ref = ref;
      refEl.textContent = ref;
      refEl.setAttribute('aria-label', `Soluzione ${ref}`);
    } else {
      refEl.remove();
    }

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
  caricaCatalogo();
  // Un link con `#impostazioni` deve aprire quella vista, non la ricerca: è il
  // link che il blocco consiglio mostra quando l'IA non è configurata.
  mostraVista(location.hash.slice(1));
  const params = new URLSearchParams(location.search);
  const daIndirizzo = params.has('origin') && params.has('destination');
  await loadProfile(!daIndirizzo);
  if (daIndirizzo) {
    await applyParams(params);
    startSearch(true);
  }
})();
