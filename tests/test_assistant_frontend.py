"""User-visible assistant flows; operator and model responses are synthetic."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from tests.test_frontend import PREPARA, sito  # noqa: F401 - shared static server fixture


@pytest.fixture
def assistant_page(sito):
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        state = {
            "requests": [], "messages": [], "proposals": [], "saved": [],
            "connections": [
                {"id": "codex", "name": "Codex", "kind": "codex", "enabled": True},
                {"id": "claude", "name": "Claude Code", "kind": "claude", "enabled": True},
            ], "routing": {},
        }

        def route_api(route):
            request = route.request
            path = urlsplit(request.url).path
            body = request.post_data_json if request.post_data else None
            state["requests"].append((request.method, path, body, request.headers))
            conversation = {"id": "c1", "title": "Viaggio sintetico", "messages": state["messages"]}
            result = {}
            if path == "/api/profile": result = {"preferences": {}, "discounts": [], "searches": []}
            elif path == "/api/tessere": result = {"tessere": []}
            elif path == "/api/ai/keys/status": result = {"providers": [], "allow_paid": False}
            elif path == "/api/ai/models": result = {"configured": False, "tasks": {}}
            elif path == "/api/agent/ui/session": result = {"csrf_token": "test-csrf"}
            elif path == "/api/agent/connections": result = {"connections": state["connections"], "routing": state["routing"]}
            elif path == "/api/agent/routing": state["routing"] = body; result = body
            elif path == "/api/agent/conversations": result = conversation if request.method == "POST" else {"conversations": [conversation]}
            elif path == "/api/agent/conversations/c1": result = conversation
            elif path == "/api/agent/conversations/c1/messages":
                state["messages"] += [{"role": "user", "content": body["text"]}, {"role": "assistant", "content": "Per quando vuoi partire?"}]
                result = {"conversation": conversation}
            elif path == "/api/agent/proposals": result = {"proposals": state["proposals"]}
            elif path == "/api/agent/proposals/p1/confirm": state["proposals"] = []; result = {"status": "confirmed"}
            elif path == "/api/agent/saved-itineraries": result = {"itineraries": state["saved"]}
            elif path == "/api/agent/operations": result = {"operations": []}
            elif path == "/api/agent/setup": result = {"config": {"mcp_servers": {"tripfinder": {"command": "TripFinder.exe", "args": ["--mcp-stdio"]}}}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

        page.route("**/api/**", route_api)
        page.goto(sito)
        yield page, state
        browser.close()


def test_chat_from_zero_asks_missing_date_and_keeps_local_history(assistant_page):
    from patchright.sync_api import expect

    page, state = assistant_page
    page.get_by_role("button", name="Chat viaggi", exact=True).click()
    page.get_by_label("Il tuo messaggio").fill("Cerca da Torino a Matera")
    page.get_by_role("button", name="Invia", exact=True).click()
    expect(page.get_by_text("Per quando vuoi partire?", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Invia", exact=True)).to_be_enabled()
    calls = [item for item in state["requests"] if item[1].endswith("/messages")]
    assert len(calls) == 1
    assert calls[0][2]["text"] == "Cerca da Torino a Matera"
    assert calls[0][3]["x-tripfinder-csrf"] == "test-csrf"


def test_explicit_task_routes_do_not_get_hidden_fallbacks(assistant_page):
    from patchright.sync_api import expect

    page, state = assistant_page
    page.get_by_role("button", name="Impostazioni", exact=True).click()
    page.get_by_label("Motore per Interpretazione", exact=True).select_option("codex")
    page.get_by_label("Motore per Chat viaggi", exact=True).select_option("claude")
    page.get_by_role("button", name="Salva attività", exact=True).click()
    expect(page.get_by_text("Attività salvate. I fallback sono soltanto quelli indicati.")).to_be_visible()
    assert state["routing"]["parse"] == {"primary": "codex", "fallbacks": []}
    assert state["routing"]["chat"] == {"primary": "claude", "fallbacks": []}
    expect(page.get_by_text("Hermes Agent: aggiungi un endpoint compatibile", exact=False)).to_be_visible()


def test_profile_requires_explicit_confirmation_and_escapes_model_text(assistant_page):
    from patchright.sync_api import expect

    page, state = assistant_page
    state["proposals"] = [{"id": "p1", "kind": "preferences", "status": "pending", "payload": {"home": '<img src=x onerror="window.pwned=true">'}}]
    page.get_by_role("button", name="Profilo", exact=True).click()
    expect(page.get_by_role("button", name="Conferma", exact=True)).to_be_visible()
    assert not [item for item in state["requests"] if item[1].endswith("/confirm")]
    assert page.locator("#agent-proposals img").count() == 0
    page.get_by_role("button", name="Conferma", exact=True).click()
    expect(page.get_by_text("Nessuna proposta in attesa.")).to_be_visible()
    assert len([item for item in state["requests"] if item[1].endswith("/confirm")]) == 1


def test_mcp_setup_keeps_generated_configuration_visible(assistant_page):
    from patchright.sync_api import expect

    page, _ = assistant_page
    page.get_by_role("button", name="Impostazioni", exact=True).click()
    page.get_by_role("button", name="Mostra configurazione MCP", exact=True).click()
    expect(page.locator("#agent-mcp-config")).to_be_visible()
    expect(page.locator("#agent-mcp-config")).to_contain_text("--mcp-stdio")


def test_chat_layout_works_on_narrow_screen(assistant_page):
    from patchright.sync_api import expect

    page, _ = assistant_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Chat viaggi", exact=True).click()
    expect(page.get_by_label("Il tuo messaggio")).to_be_visible()
    expect(page.get_by_role("button", name="Invia", exact=True)).to_be_visible()
    overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
      .filter(el => el.getBoundingClientRect().right > innerWidth + 1 && el.getBoundingClientRect().width)
      .map(el => ({tag:el.tagName,id:el.id,cls:el.className,width:el.getBoundingClientRect().width}))""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), overflow


def test_chat_import_keeps_executed_query_when_form_changes(assistant_page):
    from patchright.sync_api import expect

    page, state = assistant_page
    page.evaluate(PREPARA, 1, isolated_context=False)
    button = page.get_by_role("button", name="Continua in Chat viaggi", exact=True)
    expect(button).to_be_disabled()
    page.evaluate("""() => {
      const panel=panels[0];
      panel.outbound=[panel.trips[0].out];
      panel.queries.out={origin:'Torino',destination:'Matera',date:'2026-08-07',
        pax:2,modes:['bus'],max_budget:120,allow_night:false,max_changes:1,
        arrive_by:'21:00:00',weights:{price:1.8},origin_nodes:['stop-originale']};
      panel.updateChatAvailability();
      form.elements.pax.value='9'; form.elements.budget.value='999';
    }""", isolated_context=False)
    button.click()
    expect(page.get_by_role("heading", name="Organizziamo il viaggio")).to_be_visible()
    context = [item[2]["context"] for item in state["requests"]
               if item[0] == "POST" and item[1] == "/api/agent/conversations"][-1]
    assert context["query"]["pax"] == 2
    assert context["query"]["max_budget"] == 120
    assert context["query"]["arrive_by"] == "21:00:00"
    assert context["query"]["origin_nodes"] == ["stop-originale"]
