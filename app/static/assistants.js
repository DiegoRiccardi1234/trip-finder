'use strict';

/* Gli assistenti condividono dati e operazioni con l'app aperta. Il browser
 * conserva solo la conversazione selezionata per questa scheda. */
(() => {
  const $ = (id) => document.getElementById(id);
  if (!$('agent-form')) return;
  const tasks = { parse: 'Interpretazione', planning: 'Scali facoltativi', advice: 'Consigli e confronti', chat: 'Chat viaggi' };
  const labels = { codex: 'Codex', claude: 'Claude Code', openai_compatible: 'Endpoint compatibile / Hermes' };
  const errors = {
    not_authenticated: 'Accedi dal client ufficiale, poi ricontrolla la connessione.',
    cli_not_found: 'Il client ufficiale non è installato o non è raggiungibile.',
    not_installed: 'Il client ufficiale non è installato o non è raggiungibile.',
    protocol_error: 'Il client non ha completato il protocollo. Controlla la versione dalla diagnostica.',
    tools_disabled: 'Il runtime non consente la modalità richiesta. Controlla la versione del client.',
    busy: 'La connessione è occupata. Attendi il completamento o interrompi la richiesta in corso.',
    invalid_routing: 'Controlla le attività: abilita prima le connessioni scelte e indica solo identificativi validi nei fallback.',
    invalid_connection: 'Controlla nome, tipo, indirizzo e modello della connessione.',
    chat_runtime_failed: 'Il motore non ha completato la risposta. Controlla la connessione nelle Impostazioni.',
    chat_timeout: 'La chat ha esaurito il tempo disponibile. I risultati già raccolti restano disponibili.',
    chat_cancelled: 'Conversazione interrotta.',
    quota_exhausted: 'La quota del tuo account è esaurita. Non sono stati attivati altri provider.',
    subscription_required: 'Serve un abbonamento attuale idoneo per questo runtime.',
    timeout: 'Tempo disponibile esaurito. Puoi riprovare.', cancelled: 'Operazione interrotta.',
    not_configured: 'Scegli una connessione per questa attività nelle Impostazioni.',
    connection_unavailable: 'La connessione scelta è disattivata o incompleta. Controlla le Impostazioni.',
    failed: 'Il motore non ha completato la risposta. Controlla la connessione e riprova.',
    truncated: 'Il motore ha interrotto la risposta prima di completarla. Nessuna modifica applicata.',
    profile_changed_propose_again: 'Il profilo è cambiato: chiedi una proposta aggiornata prima di confermare.',
    search_changed_propose_again: 'I risultati sono cambiati: prepara di nuovo il salvataggio.',
    proposal_expired: 'La proposta è scaduta: chiedine una nuova.',
    invalid_search_context: 'Attendi i risultati della ricerca prima di continuare in chat.',
    conversation_busy: 'Questa conversazione sta già lavorando. Attendi o interrompi la richiesta.',
    stale_proposal: 'Il profilo è cambiato: chiedi una proposta aggiornata.',
    invalid_json: 'Il motore non ha restituito dati validi. Nessuna modifica applicata.',
    app_unavailable: 'Apri Trip Finder e riprova.',
  };
  let csrf = null;
  let selected = null;
  let busy = false;
  let refreshTimer = null;
  let connectionState = { connections: [], routing: {} };
  const operationElements = new Map();

  function messageOf(error) {
    const raw = typeof error === 'string' ? error : error?.message || 'Operazione non riuscita.';
    return errors[raw] || raw;
  }
  function status(id, text, error = false) {
    $(id).textContent = text;
    $(id).dataset.error = String(error);
  }
  async function bootstrap() {
    if (csrf) return;
    const response = await fetch('/api/agent/ui/session', {
      method: 'POST', credentials: 'same-origin', headers: { 'x-tripfinder-ui': '1' },
    });
    if (!response.ok) throw new Error('Non riesco ad aprire la sessione locale. Ricarica la pagina.');
    const data = await response.json();
    csrf = data.csrf_token || data.csrf;
  }
  async function api(path, options = {}) {
    const mutation = options.method && options.method !== 'GET';
    await bootstrap();
    const response = await fetch(`/api/agent${path}`, {
      ...options, credentials: 'same-origin', headers: {
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(mutation ? { 'x-tripfinder-csrf': csrf } : {}), ...options.headers,
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 403) csrf = null;
      const detail = data.detail;
      throw new Error(typeof detail === 'string' ? detail : detail?.message || detail?.code || data.error || 'Operazione non riuscita.');
    }
    return data;
  }
  const post = (path, data = {}) => api(path, { method: 'POST', body: JSON.stringify(data) });
  const listOf = (data, key) => Array.isArray(data) ? data : data?.[key] || [];
  const uid = () => crypto.randomUUID();

  function action(text, handler, primary = false) {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = text;
    if (!primary) button.className = 'link';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await handler(); } catch (error) { status('agent-state', messageOf(error), true); }
      finally { button.disabled = false; }
    });
    return button;
  }
  function message(role, content) {
    const item = document.createElement('article');
    item.className = `assistant-message assistant-message--${role}`;
    const name = document.createElement('strong'); name.textContent = role === 'user' ? 'Tu' : 'Trip Finder';
    const text = document.createElement('p'); text.textContent = content;
    item.append(name, text); return item;
  }
  function renderConversation(value) {
    const conversation = value.conversation || value;
    if (!conversation.id) return;
    selected = conversation.id;
    const box = $('agent-messages'); box.replaceChildren();
    const messages = conversation.messages || [];
    if (!messages.length) {
      const hint = document.createElement('p'); hint.className = 'hint';
      hint.textContent = conversation.context?.operation_id ? 'I risultati sono nel contesto. Chiedimi di confrontarli o di cambiare il viaggio.' : 'Dimmi da dove parti, dove vuoi andare e quando.';
      box.append(hint);
    }
    for (const entry of messages) {
      if (['user', 'assistant'].includes(entry.role)) box.append(message(entry.role, entry.content || entry.text || ''));
    }
    box.scrollTop = box.scrollHeight;
    const operations = value.operations || conversation.operations || [];
    for (const operation of operations) {
      if (typeof operation === 'string') refreshOperation(operation);
      else renderOperation(operation);
    }
    const imported = conversation.context?.operation_id;
    if (imported) refreshOperation(imported);
  }
  async function loadHistory() {
    const data = await api('/conversations');
    const box = $('agent-history'); box.replaceChildren();
    for (const conversation of listOf(data, 'conversations')) {
      const li = document.createElement('li');
      li.append(action(conversation.title || 'Conversazione', async () => {
        if (busy) return;
        $('agent-results').replaceChildren(); operationElements.clear();
        renderConversation(await api(`/conversations/${encodeURIComponent(conversation.id)}`));
        status('agent-state', 'Conversazione locale ripresa.');
      })); box.append(li);
    }
    if (!box.children.length) { const li = document.createElement('li'); li.className = 'hint'; li.textContent = 'Le conversazioni appariranno qui.'; box.append(li); }
  }
  async function newConversation(payload = {}) {
    if (busy) return;
    const data = await post('/conversations', payload);
    $('agent-results').replaceChildren(); operationElements.clear();
    renderConversation(data); await loadHistory();
    vaiA('assistente'); $('agent-input').focus();
    status('agent-state', payload.context ? 'Risultati aggiunti alla conversazione.' : 'Nuova conversazione.');
  }
  function setBusy(value) {
    busy = value; $('agent-send').disabled = value; $('agent-new').disabled = value;
    $('agent-stop').hidden = !value;
  }
  $('agent-new').addEventListener('click', () => newConversation().catch((e) => status('agent-state', messageOf(e), true)));
  window.addEventListener('tripfinder:chat-context', (event) => {
    newConversation(event.detail).catch((e) => { vaiA('assistente'); status('agent-state', messageOf(e), true); });
  });
  $('agent-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (busy) return;
    const text = $('agent-input').value.trim(); if (!text) return;
    try {
      if (!selected) await newConversation();
      setBusy(true); $('agent-input').value = '';
      $('agent-messages').append(message('user', text));
      status('agent-state', 'Sto organizzando il viaggio…');
      refreshTimer = setInterval(async () => {
        try {
          const data = await api(`/operations?conversation_id=${encodeURIComponent(selected)}`);
          for (const op of listOf(data, 'operations')) renderOperation(op);
        } catch { /* La risposta del turno mantiene l'errore principale. */ }
      }, 1500);
      const result = await post(`/conversations/${selected}/messages`, { text, request_id: uid() });
      renderConversation(result);
      status('agent-state', result.reason ? messageOf(result.reason) : 'Risposta completata.', Boolean(result.reason));
      await Promise.all([loadHistory(), loadProposals()]);
    } catch (error) {
      status('agent-state', messageOf(error), true);
      $('agent-input').value = text;
    } finally { clearInterval(refreshTimer); setBusy(false); }
  });
  $('agent-stop').addEventListener('click', async () => {
    if (!selected) return;
    try { await post(`/conversations/${selected}/cancel`); status('agent-state', 'Interruzione richiesta. I risultati già raccolti restano disponibili.'); }
    catch (error) { status('agent-state', messageOf(error), true); }
  });

  function itineraryNode(entry, operation, historical = false) {
    const itinerary = entry.itinerary || entry.snapshot || entry;
    const node = document.createElement('article'); node.className = 'assistant-itinerary';
    const legs = itinerary.legs || [];
    const title = document.createElement('h4');
    title.textContent = entry.name || `${legs[0]?.origin?.name || 'Partenza'} → ${legs.at(-1)?.destination?.name || 'Arrivo'}`;
    const summary = document.createElement('p');
    const total = itinerary.cost?.total;
    summary.textContent = `${itinerary.depart ? new Date(itinerary.depart).toLocaleString('it-IT') : ''}`
      + (Number.isFinite(total) ? ` · ${fmtMoney(total)} a persona` : '')
      + (itinerary.duration_min ? ` · ${fmtDuration(itinerary.duration_min)}` : '')
      + (itinerary.n_changes !== undefined ? ` · ${itinerary.n_changes} cambi` : '');
    node.append(title, summary);
    if (historical) {
      const note = document.createElement('p'); note.className = 'hint';
      const observed = entry.snapshot_at || entry.observed_at || entry.created_at;
      note.textContent = 'Orari e prezzi storici'
        + (observed ? `, osservati il ${new Date(typeof observed === 'number' ? observed * 1000 : observed).toLocaleString('it-IT')}` : '')
        + '. Verifica di nuovo prima di partire.'; node.append(note);
    }
    if (legs.length) {
      const details = document.createElement('details');
      const heading = document.createElement('summary'); heading.textContent = 'Tratte, operatori e costi';
      const content = document.createElement('div');
      // I renderer condivisi fanno escaping di ogni testo dell'operatore.
      content.innerHTML = `<ol class="legs">${renderLegs(legs)}</ol>${renderBreakdown(itinerary)}`;
      details.append(heading, content); node.append(details);
    }
    const actions = document.createElement('div'); actions.className = 'assistant-actions';
    if (operation && !historical) actions.append(action('Proponi salvataggio', async () => {
      await post('/proposals/save-itinerary', {
        request_id: uid(), operation_id: operation.id || operation.operation_id,
        itinerary_id: entry.id || itinerary.id, name: title.textContent.slice(0, 80),
      });
      await loadProposals(); status('agent-state', 'Proposta preparata. Conferma il salvataggio nel Profilo.'); vaiA('profilo');
    }));
    if (historical) actions.append(action('Prepara nuova ricerca', async () => {
      const query = entry.plan || entry.query || entry.search;
      if (!query) throw new Error('Questa copia non contiene i parametri per ripetere la ricerca.');
      const first = query.stages?.[0] || query;
      const params = new URLSearchParams({ origin: first.origin, destination: first.destination,
        date: first.date, pax: String(query.pax || 1),
        modes: (query.modes || ['rail', 'bus', 'air', 'ferry']).join(','),
        bag: String(Boolean(query.with_checked_bag)), allow_night: String(query.allow_night !== false),
        max_changes: String(query.max_changes ?? 3), budget: String(query.max_budget ?? ''),
        arrive_by: first.arrive_by || '', depart_after: first.depart_after || '',
      });
      await applyParams(params); vaiA('ricerca');
      const today = new Date(); today.setMinutes(today.getMinutes() - today.getTimezoneOffset());
      if (first.date < today.toISOString().slice(0, 10)) form.elements.date.value = '';
      say('Controlla la data e premi Cerca per aggiornare orari e prezzi.', 'ok');
      form.elements.date.focus();
    }));
    node.append(actions); return node;
  }
  function renderOperation(operation) {
    const id = operation.id || operation.operation_id;
    if (!id) return;
    let node = operationElements.get(id);
    if (!node) { node = document.createElement('section'); node.className = 'assistant-operation'; operationElements.set(id, node); $('agent-results').append(node); }
    node.replaceChildren();
    const heading = document.createElement('h3'); heading.textContent = operation.title || 'Risultati della ricerca';
    const state = document.createElement('p'); state.className = 'hint';
    const stateLabels = { queued: 'In coda', running: 'Ricerca in corso', completed: 'Ricerca completata', cancelled: 'Ricerca interrotta', failed: 'Ricerca non riuscita', interrupted: 'Interrotta dal riavvio' };
    state.textContent = stateLabels[operation.status] || operation.status || 'Ricerca';
    if (operation.detail || operation.error) state.textContent += ` · ${messageOf(operation.detail || operation.error)}`;
    node.append(heading, state);
    if (operation.result?.stage_dates_provisional) {
      const note = document.createElement('p'); note.className = 'hint';
      note.textContent = 'Le date delle tappe successive sono provvisorie: confermale dopo aver scelto gli itinerari e verificato arrivi e giorni di sosta.';
      node.append(note);
    }
    const entries = operation.itineraries || operation.result?.itineraries || operation.result?.items || [];
    for (const entry of entries) node.append(itineraryNode(entry, operation));
    for (const stage of operation.stages || operation.result?.stages || []) {
      const label = document.createElement('h4'); label.textContent = stage.label || `${stage.query?.origin || stage.origin || ''} → ${stage.query?.destination || stage.destination || ''}`; node.append(label);
      for (const entry of stage.itineraries || stage.items || []) node.append(itineraryNode(entry, operation));
    }
    if (['running', 'queued'].includes(operation.status)) node.append(action('Interrompi ricerca', async () => { renderOperation(await post(`/operations/${id}/cancel`)); }));
    if (!busy && ['running', 'queued'].includes(operation.status)) setTimeout(() => refreshOperation(id), 1800);
  }
  async function refreshOperation(id) {
    try { const value = await api(`/operations/${encodeURIComponent(id)}`); renderOperation(value.operation || value); }
    catch (error) { status('agent-state', messageOf(error), true); }
  }
  async function loadSaved() {
    const values = listOf(await api('/saved-itineraries'), 'itineraries');
    const box = $('agent-saved'); box.replaceChildren();
    for (const entry of values) {
      const full = entry.snapshot || entry.itinerary ? entry : await api(`/saved-itineraries/${encodeURIComponent(entry.id)}`);
      box.append(itineraryNode(full, null, true));
    }
    if (!values.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'Salva un itinerario dalla Chat viaggi o conferma una proposta del tuo assistente.'; box.append(p); }
  }
  function proposalDescription(proposal) {
    const payload = proposal.payload || proposal.data || {};
    if (proposal.kind === 'save_itinerary' || proposal.kind === 'save_search') return `Salva ${payload.name || 'questo itinerario'} con i parametri della ricerca e una copia storica dei risultati.`;
    if (proposal.kind === 'preferences') return 'Modifica le preferenze di viaggio:';
    if (proposal.kind === 'discount') return 'Modifica una tessera dichiarata:';
    return 'Controlla la modifica proposta prima di applicarla:';
  }
  async function loadProposals() {
    const proposals = listOf(await api('/proposals'), 'proposals');
    const box = $('agent-proposals'); box.replaceChildren();
    for (const proposal of proposals.filter((p) => p.status === 'pending')) {
      const node = document.createElement('article'); node.className = 'assistant-proposal';
      const title = document.createElement('strong'); title.textContent = proposalDescription(proposal);
      const detail = document.createElement('pre'); detail.textContent = JSON.stringify(proposal.payload || proposal.data || {}, null, 2);
      const buttons = document.createElement('div'); buttons.className = 'assistant-actions';
      buttons.append(action('Conferma', async () => {
        await post(`/proposals/${proposal.proposal_id || proposal.id}/confirm`); await Promise.all([loadProposals(), loadSaved(), loadProfile(false)]);
      }, true), action('Rifiuta', async () => { await post(`/proposals/${proposal.proposal_id || proposal.id}/reject`); await loadProposals(); }));
      node.append(title, detail, buttons); box.append(node);
    }
    if (!box.children.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'Nessuna proposta in attesa.'; box.append(p); }
  }

  function connectionsList() {
    return Array.isArray(connectionState.connections) ? connectionState.connections : Object.values(connectionState.connections || {});
  }
  function editConnection(connection) {
    const editor = $('agent-connection-form');
    for (const key of ['id', 'name', 'kind', 'base_url', 'model', 'timeout_seconds']) {
      if (editor.elements[key]) editor.elements[key].value = connection[key] ?? (key === 'timeout_seconds' ? 120 : '');
    }
    editor.elements.api_key.value = ''; editor.elements.clear_api_key.checked = false;
    editor.elements.enabled.checked = Boolean(connection.enabled);
    $('agent-connection-editor').open = true; editor.elements.name.focus();
  }
  function renderConnections() {
    const box = $('agent-connections'); box.replaceChildren();
    const connections = connectionsList();
    for (const connection of connections) {
      const row = document.createElement('div'); row.className = 'connection-row';
      const text = document.createElement('div'); const title = document.createElement('strong');
      title.textContent = connection.name || labels[connection.kind] || connection.id;
      const note = document.createElement('p'); note.className = 'hint';
      note.textContent = `${connection.enabled ? 'Abilitata' : 'Disattivata'} · ${labels[connection.kind] || connection.kind}`
        + (connection.model ? ` · ${connection.model}` : '')
        + (connection.secret_configured || connection.has_secret ? ' · chiave salvata' : ''); text.append(title, note);
      const buttons = document.createElement('div'); buttons.className = 'assistant-actions';
      if (connection.kind !== 'legacy_api') buttons.append(action('Modifica', async () => editConnection(connection)));
      if (['codex', 'claude'].includes(connection.kind)) buttons.append(action('Diagnostica', async () => {
        const result = await api(`/runtimes/${connection.kind}`);
        const quota = result.quota;
        status('agent-settings-state', `${title.textContent}: ${result.authenticated ? 'accesso presente' : messageOf(result.reason || result.error || 'Accesso non verificato')}`
          + (result.version ? ` · ${result.version}` : '')
          + (quota ? ` · quota: ${JSON.stringify(quota)}` : ' · quota non disponibile'));
      }));
      row.append(text, buttons); box.append(row);
    }
    const hermes = document.createElement('p'); hermes.className = 'hint';
    hermes.textContent = 'Hermes Agent: aggiungi un endpoint compatibile già configurato, oppure collegalo come client MCP. Nessun gateway viene installato o attivato da Trip Finder.'; box.append(hermes);
    const routing = $('agent-routing'); routing.replaceChildren();
    for (const [task, label] of Object.entries(tasks)) {
      const row = document.createElement('div'); row.className = 'routing-row'; row.dataset.task = task;
      const name = document.createElement('label'); name.textContent = label; name.htmlFor = `agent-route-${task}`;
      const select = document.createElement('select'); select.id = `agent-route-${task}`;
      select.setAttribute('aria-label', `Motore per ${label}`);
      const legacy = document.createElement('option'); legacy.value = ''; legacy.textContent = 'Provider API esistenti'; select.append(legacy);
      for (const connection of connections) {
        const option = document.createElement('option'); option.value = connection.id;
        option.textContent = (connection.name || connection.id) + (!connection.enabled ? ' (disattivata)' : '');
        option.disabled = !connection.enabled; select.append(option);
      }
      const route = connectionState.routing?.[task] || {};
      select.value = route.primary || '';
      const fallback = document.createElement('input'); fallback.className = 'route-fallbacks';
      fallback.placeholder = 'Fallback: id separati da virgola'; fallback.setAttribute('aria-label', `Fallback espliciti per ${label}`);
      fallback.value = (route.fallbacks || []).join(', '); row.append(name, select, fallback); routing.append(row);
    }
  }
  async function loadConnections() { connectionState = await api('/connections'); renderConnections(); }
  $('agent-connection-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const editor = event.currentTarget; const values = Object.fromEntries(new FormData(editor));
    values.enabled = editor.elements.enabled.checked; values.timeout_seconds = Number(values.timeout_seconds);
    values.clear_api_key = editor.elements.clear_api_key.checked;
    if (!values.api_key) delete values.api_key;
    try { await post('/connections', values); editor.elements.api_key.value = ''; editor.elements.clear_api_key.checked = false; await loadConnections(); status('agent-settings-state', 'Connessione salvata. Scegli le attività a cui assegnarla.'); }
    catch (error) { status('agent-settings-state', messageOf(error), true); }
  });
  $('agent-routing-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const routing = {};
    for (const row of $('agent-routing').children) routing[row.dataset.task] = {
      primary: row.querySelector('select').value || null,
      fallbacks: row.querySelector('input').value.split(',').map((v) => v.trim()).filter(Boolean),
    };
    try { await api('/routing', { method: 'PUT', body: JSON.stringify(routing) }); await loadConnections(); status('agent-settings-state', 'Attività salvate. I fallback sono soltanto quelli indicati.'); }
    catch (error) { status('agent-settings-state', messageOf(error), true); }
  });
  $('agent-mcp-setup').addEventListener('click', async () => {
    try { const data = await post('/setup', { client: $('agent-mcp-client').value });
      const client = $('agent-mcp-client').value;
      const config = data.configs?.[client] || data.configs?.[client === 'claude' ? 'claude_desktop' : client] || data.config || data;
      $('agent-mcp-config').textContent = typeof config === 'string' ? config : JSON.stringify(config, null, 2); $('agent-mcp-config').hidden = false;
    } catch (error) { status('agent-settings-state', messageOf(error), true); }
  });
  async function onView(name) {
    try {
      if (name === 'assistente') { await loadHistory(); if (selected) renderConversation(await api(`/conversations/${selected}`)); }
      if (name === 'profilo') await Promise.all([loadSaved(), loadProposals()]);
      if (name === 'impostazioni') await loadConnections();
    } catch (error) { status(name === 'impostazioni' ? 'agent-settings-state' : 'agent-state', messageOf(error), true); }
  }
  window.addEventListener('tripfinder:view', (event) => onView(event.detail));
  onView(location.hash.slice(1));
})();
