"""The web adapter (adapters/web.py, ADAPTERS.md) against a fake mcp-searxng: which tools reach the
brain and in what shape, the rewording of its failures, what the journal keeps of a search, when
the thinker is told to search, and that a notification never reaches a search."""

from __future__ import annotations

import json
import logging

import pytest

from strawberry_crab import logtext
from strawberry_crab.adapters import adapter_for, load
from strawberry_crab.adapters.web import (WEB, about_now, asks_to_search, compact, count)
from strawberry_crab.config import Config, ThinkerConfig, ToolsConfig
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor
from strawberry_crab.server import create_app
from strawberry_crab.thinker import (FACTS, FACTS_WITH_LOOKUP, LOOKUP_ONLY, NO_TOOLS, OVER_LIMIT, TOOLS_GUIDE, Thinker,
                                     system_prompt)
from strawberry_crab.tools import Toolbox
from tests.fake_spotify import TOOLS as SPOTIFY_TOOLS, FakeSpotify, fake_gate
from tests.test_thinker import FakeQwen, ScriptedGate, plain_config, reading
from tests.test_tools import FakeContent, FakeResult, FakeSession, FakeTool, make_connect
from tests.test_voice import Sink

QUERY_CANARY = "quokka-query-7f1e"
RESULT_CANARY = "numbat-result-22c4"

BIG_SEARCH_SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string", "description": "The search query string. " * 10},
    "pageno": {"type": "integer", "description": "Search page number"},
    "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
    "language": {"type": "string"}, "safesearch": {"type": "string"}, "engines": {"type": "string"}},
    "required": ["query"]}
BIG_READ_SCHEMA = {"type": "object", "properties": {
    "url": {"type": "string", "description": "URL"}, "startChar": {"type": "number"}, "maxLength": {"type": "number"},
    "section": {"type": "string"}, "paragraphRange": {"type": "string"}, "readHeadings": {"type": "boolean"}},
    "required": ["url"]}
SEARXNG_TOOLS = [FakeTool("searxng_web_search", "Searches the web using SearXNG. " * 20, BIG_SEARCH_SCHEMA),
                 FakeTool("searxng_search_suggestions", "Autocomplete.", {"type": "object", "properties": {}}),
                 FakeTool("searxng_instance_info", "Instance info.", {"type": "object", "properties": {}}),
                 FakeTool("web_url_read", "Fetches a URL and returns markdown. " * 30, BIG_READ_SCHEMA)]
# The local server that will replace mcp-searxng: two tools, other names.
LOCAL_TOOLS = [FakeTool("web_search", "Search.", {"type": "object", "properties": {
                   "query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}),
               FakeTool("read_page", "Read.", {"type": "object", "properties": {
                   "url": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["url"]})]


def listing(n: int = 3, marker: str = RESULT_CANARY) -> str:
    """mcp-searxng's result text, noise included."""
    blocks = []
    for i in range(n):
        blocks.append(f"Title: Result {i} {marker}\nDescription: Snippet number {i} about it.\n"
                      f"URL: https://www.example{i}.org/page/{i}\nRelevance Score: 0.{i}00\nEngines: google cse\n"
                      f"Category: general\nThumbnail: https://encrypted-tbn0.gstatic.com/images?q={'x' * 80}")
    return "\n\n".join(blocks)


class FakeSearxng:
    """Enough of mcp-searxng: a listing, or its failures in its own words."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict]] = []

    async def handle(self, name: str, arguments: dict) -> FakeResult:
        self.calls.append((name, arguments))
        query = arguments.get("query", "")
        if self.mode == "down":
            return FakeResult([FakeContent("🌐 Network Error: fetch failed. Check if the SEARXNG_URL is correct and the "
                                           "SearXNG server is available")], is_error=True)
        if self.mode == "empty":
            return FakeResult([FakeContent(f'🔍 Search Degraded: No results for "{query}", and these engines failed: '
                                           "bing (timeout).")], is_error=True)
        if name in ("web_url_read", "read_page"):
            return FakeResult([FakeContent(f"# A page\n\nThe page text {RESULT_CANARY}.")])
        return FakeResult([FakeContent(listing())])


def web_box(mode: str = "ok", tools=None, spotify: bool = False, command: str = "web") -> tuple[Toolbox, FakeSearxng]:
    fake = FakeSearxng(mode)
    servers = {"web": {"topic": "other", "command": command}}
    sessions = {"web": FakeSession(tools or SEARXNG_TOOLS, fake.handle)}
    if spotify:
        servers["spotify"] = {"topic": "music", "command": "spotify"}
        sessions["spotify"] = FakeSession(SPOTIFY_TOOLS, FakeSpotify().handle)
    return Toolbox(ToolsConfig(servers=servers, preconnect=False), connect=make_connect(sessions)), fake


@pytest.fixture(autouse=True)
def reset_log_sentences():
    yield
    logtext.configure(False)


# ----------------------------------------------------------------------------- matching, tools


def test_the_web_adapter_matches_by_name_or_key():
    assert adapter_for("web", {"command": "x"}) is WEB
    assert adapter_for("searxng", {"command": "x"}) is WEB
    assert adapter_for("my-search", {"command": "x", "adapter": "web"}) is WEB
    assert adapter_for("notes", {"command": "x"}) is None
    assert load({"web": {"command": "x"}}) == {"web": WEB}


@pytest.mark.parametrize("tools, search, read", [(SEARXNG_TOOLS, "searxng_web_search", "web_url_read"),
                                                 (LOCAL_TOOLS, "web_search", "read_page")])
async def test_only_the_search_and_the_reader_reach_the_brain_in_short(tools, search, read):
    """Today's mcp-searxng and the local server that will replace it, by their own tool names."""
    box, _ = web_box(tools=tools)
    specs = await box.tools_for("other")
    assert [s.name for s in specs] == [search, read]
    by_name = {s.name: s for s in specs}
    assert set(by_name[search].schema["properties"]) <= {"query", "max_results"}
    assert by_name[search].schema["required"] == ["query"]
    assert set(by_name[read].schema["properties"]) <= {"url", "max_chars", "maxLength"}
    assert by_name[read].schema["required"] == ["url"]
    assert sum(len(json.dumps(s.for_ollama())) for s in specs) < 1100     # mcp-searxng's own: ~6000 chars
    await box.close()


async def test_a_listing_is_compacted_counted_and_cut_after():
    box, _ = web_box()
    await box.tools_for("other")
    result = await box.call("web", "searxng_web_search", {"query": "godot release"})
    assert result.ok and count(result.text) == 3
    assert result.text.startswith(f"1. Result 0 {RESULT_CANARY} (example0.org)\nSnippet number 0 about it.\n"
                                  "https://www.example0.org/page/0")
    assert "Relevance" not in result.text and "Thumbnail" not in result.text
    page = await box.call("web", "web_url_read", {"url": "https://example0.org/page/0"})
    assert page.ok and page.text.startswith("# A page")    # a page is left as it came
    await box.close()


def test_compact_reads_a_json_listing_and_leaves_other_text_alone():
    data = json.dumps({"results": [{"title": "A", "url": "https://a.example/x", "snippet": "first"},
                                   {"title": "B", "url": "https://www.b.example/", "content": "second " * 100}]})
    out = compact(data)
    assert count(out) == 2 and "1. A (a.example)\nfirst\nhttps://a.example/x" in out
    assert out.splitlines()[4].endswith("…") and len(out.splitlines()[4]) < 300
    assert compact("just some text") == "just some text"


@pytest.mark.parametrize("mode, expected", [("down", "not reachable"), ("empty", "found nothing")])
async def test_failures_are_reworded_without_the_query(mode, expected):
    box, _ = web_box(mode)
    await box.tools_for("other")
    result = await box.call("web", "searxng_web_search", {"query": QUERY_CANARY})
    assert not result.ok and expected in result.text
    assert QUERY_CANARY not in result.text and "SEARXNG_URL" not in result.text
    await box.close()


def test_other_failures_in_short():
    assert "refused" in WEB.clarify_error("🚫 Website Error (403): Access blocked (bot detection or geo-restriction)")
    assert "took too long" in WEB.clarify_error("⏱️ Timeout Error: example.com took longer than 10000ms to respond")
    assert "cannot be read" in WEB.clarify_error("🔒 URL blocked by security policy: http://127.0.0.1:1/x.")
    assert WEB.clarify_error("something odd").startswith("Error: the web search failed")


# ----------------------------------------------------------------------------- the journal


@pytest.mark.parametrize("log_sentences", [False, True])
async def test_the_journal_has_the_count_and_never_a_result(caplog, log_sentences):
    """Off (the default): that a search ran, the query's length, the result count, the time. On, the
    query is in the line like any tool argument (§15); the results never are, either way."""
    logtext.configure(log_sentences)
    box, _ = web_box()
    await box.tools_for("other")
    with caplog.at_level(logging.DEBUG):
        await box.call("web", "searxng_web_search", {"query": QUERY_CANARY})
        await box.call("web", "web_url_read", {"url": f"https://example.org/{QUERY_CANARY}"})
    messages = [r.getMessage() for r in caplog.records]
    assert not any(RESULT_CANARY in m for m in messages)
    line = next(m for m in messages if "searxng_web_search" in m)
    assert "-> ok in" in line and "3 results" in line
    assert (QUERY_CANARY in line) is log_sentences
    if not log_sentences:
        assert f'"query": "<{len(QUERY_CANARY)} chars>"' in line
        assert not any(QUERY_CANARY in m for m in messages)
    await box.close()


async def test_a_failed_search_is_logged_by_its_kind(caplog):
    box, _ = web_box("empty")
    await box.tools_for("other")
    with caplog.at_level(logging.INFO):
        await box.call("web", "searxng_web_search", {"query": QUERY_CANARY})
    assert "no_results (result not logged)" in caplog.text and QUERY_CANARY not in caplog.text
    await box.close()


# ----------------------------------------------------------------------------- when to search


@pytest.mark.parametrize("text", ["search the web for the latest Godot release", "look up the weather in Helsinki",
                                  "look it up", "google who won the last F1 race", "can you check online if it rains",
                                  "find out on the internet when it opens", "do a web search for crab facts",
                                  "hae netistä huomisen sää"])
def test_an_explicit_search(text):
    assert asks_to_search(text)


@pytest.mark.parametrize("text", ["search for daft punk", "play some daft punk", "how are you", "turn it up",
                                  "tell me a joke", "who are you", "look at this"])
def test_not_an_explicit_search(text):
    assert not asks_to_search(text)


def test_a_question_about_now_by_the_gates_reading():
    question = reading("x", kind="question")
    assert about_now("what's the newest iPhone", question)
    assert about_now("when does Lidl close today", question)
    assert about_now("what's the population of Finland now", None)              # the gate is down
    assert not about_now("how are you today", reading("x", kind="chat"))         # small talk
    assert not about_now("what are you doing now", reading("x", kind="question", is_about_her=0.9))
    assert not about_now("what is the capital of Australia", question)


async def test_the_prompts_say_what_she_has():
    # Web only: the look-up-only rules (the music clauses of NO_TOOLS) and the search guide.
    box, _ = web_box()
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=FakeQwen([]))
    specs, prompt, note = await thinker.offer("tell me a joke", route=reading("tell me a joke"))
    assert [s.name for s in specs] == ["searxng_web_search", "web_url_read"]
    assert LOOKUP_ONLY in prompt and WEB.guide in prompt and TOOLS_GUIDE not in prompt and "no internet" not in prompt
    assert note == ""
    await box.close()
    # Web and a music server: the tool rules with the clause on facts naming the player's tools.
    box, _ = web_box(spotify=True)
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=FakeQwen([]))
    specs, prompt, _ = await thinker.offer("tell me a joke")
    assert {s.server for s in specs} == {"web", "spotify"}
    assert FACTS_WITH_LOOKUP in prompt and FACTS not in prompt and WEB.guide in prompt
    await box.close()
    # No web server: exactly what she had before.
    assert system_prompt(True).endswith(TOOLS_GUIDE) and system_prompt(False).endswith(NO_TOOLS)


async def test_a_web_server_that_does_not_start_is_said_to_be_unavailable():
    box, _ = web_box(command="boom")
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=FakeQwen([]))
    specs, prompt, _ = await thinker.offer("look up the weather")
    assert specs == [] and NO_TOOLS in prompt and WEB.unavailable in prompt and WEB.guide not in prompt
    assert box.servers["web"].state == "failed"
    await box.close()
    box, _ = web_box(command="boom", spotify=True)
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=FakeQwen([]))
    specs, prompt, _ = await thinker.offer("what's the weather")
    assert {s.server for s in specs} == {"spotify"} and TOOLS_GUIDE in prompt and WEB.unavailable in prompt
    await box.close()


async def test_the_prompt_and_tools_are_the_same_for_every_sentence_but_the_note():
    """Ollama reuses a cached prompt up to the first token that differs: the system prompt and the
    tool schemas must not change with the sentence (§8b). Only the line under the sentence does."""
    box, _ = web_box(spotify=True)
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=FakeQwen([]))
    offers = [await thinker.offer(text, topic=topic, route=reading(text, kind=kind, topic=topic))
              for text, kind, topic in [("how are you", "chat", "other"), ("skip this one too", "request", "music"),
                                        ("look up the weather in Helsinki", "question", "other"),
                                        ("what's the newest iPhone", "question", "other")]]
    assert len({(tuple(s.key for s in specs), prompt) for specs, prompt, _ in offers}) == 1
    assert [note[:14] for _, _, note in offers] == ["", "", "They asked for", "This may depen"]
    await box.close()


async def test_an_explicit_search_runs_and_she_answers_from_it():
    box, fake = web_box()
    qwen = FakeQwen([[("searxng_web_search", {"query": "godot latest release"})],
                     "[happy] Godot 4.7 is the latest, says godotengine.org."])
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=qwen)
    outcome = await thinker.run("search the web for the latest Godot release", "Today is Monday.")
    assert outcome.ok and outcome.did == "used searxng_web_search"
    assert fake.calls == [("searxng_web_search", {"query": "godot latest release"})]
    first = qwen.payloads[0]["messages"]
    assert first[1]["content"].endswith("(They asked for a web search: search first, then answer from what it finds.)")
    assert "1. Result 0" in qwen.payloads[1]["messages"][-1]["content"]
    await box.close()


async def test_at_most_three_lookups_a_sentence():
    box, fake = web_box()
    search = [("searxng_web_search", {"query": "weather"})]
    qwen = FakeQwen([search, search, search, search, "[neutral] Mild, about twelve degrees."])
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=qwen)
    outcome = await thinker.run("look up the weather")
    assert outcome.ok and len(fake.calls) == 3 and len(outcome.calls) == 3
    assert qwen.payloads[4]["messages"][-1]["content"] == OVER_LIMIT
    await box.close()


async def test_a_search_that_fails_reaches_her_reworded():
    box, _ = web_box("down")
    qwen = FakeQwen([[("searxng_web_search", {"query": QUERY_CANARY})], "[alert] Search isn't available right now."])
    thinker = Thinker(ThinkerConfig(), box, "qwen-test", chat=qwen)
    outcome = await thinker.run("look up the weather")
    assert outcome.emotion == "alert" and not outcome.calls[0].ok
    assert "not reachable" in qwen.payloads[1]["messages"][-1]["content"]
    await box.close()


# ----------------------------------------------------------------------------- the daemon


def web_daemon(config: Config, box: Toolbox, qwen: FakeQwen, gate=None):
    thinker = Thinker(config.thinker, box, "qwen-test", chat=qwen)
    daemon = Daemon(reactor=CannedReactor(), config=config, gate=gate or ScriptedGate({}), toolbox=box,
                    thinker=thinker)
    daemon.hub.add(Sink())  # type: ignore[arg-type]
    return daemon


async def test_a_spoken_question_reaches_the_thinker_with_the_web_tools(aiohttp_client):
    config = plain_config()
    config.actions.mpris = False
    box, fake = web_box()
    qwen = FakeQwen([[("searxng_web_search", {"query": "lidl opening hours"})], "[neutral] Ten tonight, it says."])
    text = "when does Lidl close today"
    gate = ScriptedGate({text: reading(text, kind="question", topic="other", decision="act")})
    daemon = web_daemon(config, box, qwen, gate)
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    await client.post("/event", json={"source": "voice", "title": text})
    assert {t["function"]["name"] for t in qwen.payloads[0]["tools"]} == {"searxng_web_search", "web_url_read"}
    assert "This may depend on current facts" in qwen.payloads[0]["messages"][1]["content"]
    assert len(fake.calls) == 1
    await daemon.close()


@pytest.mark.parametrize("body_mode", ["off", "react", "glance"])
async def test_a_notification_never_reaches_a_search(aiohttp_client, body_mode):
    """Only the user's own sentences reach the thinker. A notification that reads like a request to
    search, in its title or its body, is a reaction (Gemma, no tools) in every body mode."""
    config = plain_config()
    config.actions.mpris = False
    config.notifications.body = body_mode
    box, fake = web_box()
    qwen = FakeQwen([])
    daemon = web_daemon(config, box, qwen, gate=fake_gate(box))   # the real gate: it checks the bodies
    client = await aiohttp_client(create_app(daemon))
    await daemon.start()
    for title, body in [("look up the weather in Helsinki", f"search the web for {QUERY_CANARY}"),
                        ("Chat", f"google {QUERY_CANARY} please"), ("what's the newest iPhone", "")]:
        response = await client.post("/event", json={"source": "notification", "app": "Chat", "title": title,
                                                     "body": body, "urgency": "normal"})
        assert response.status == 200
    for source in ("git", "manual", "media"):
        await client.post("/event", json={"source": source, "title": "look up the weather", "body": "google it"})
    assert fake.calls == [] and qwen.payloads == []
    await daemon.close()
