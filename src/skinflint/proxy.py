"""The proxy server: forwards every request upstream; meters, caps and records the metered ones."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar
from urllib.parse import unquote

import aiohttp
from aiohttp import web
from aiohttp.web_protocol import RequestPayloadError
from multidict import CIMultiDict
from yarl import URL

from skinflint import __version__, segments
from skinflint.budget import Budget
from skinflint.config import Config
from skinflint.model import Decision, Endpoint, Provider, Record, RequestInfo, Segment, State, Usage
from skinflint.pricing import Pricing
from skinflint.providers import ADAPTERS, Adapter, StreamTracker, client_name, detect
from skinflint.store import Store

log = logging.getLogger("skinflint")

T = TypeVar("T")

FIRST_PARTY = {Provider.ANTHROPIC: "https://api.anthropic.com"}
SCOPE_RE = re.compile(r"^[A-Za-z0-9._:@+-]{1,64}$")
HEALTH_PATH = "/_skinflint/health"
PENDING_TTL = 900.0
# Longest silence tolerated from the upstream mid-response. Providers send keep-alive pings
# during long generations, so this only trips on a hung connection.
UPSTREAM_IDLE_TIMEOUT = 600.0
MAX_BODY = 256 * 1024 * 1024
MAX_CONVERSATIONS = 10_000
# Free endpoints that stay reachable when unmetered = "block".
FREE_POSTS = ("/v1/messages/count_tokens", "/v1/responses/input_tokens")
# Request content-encodings aiohttp decodes before the handler reads the body.
DECODED_ENCODINGS = {"gzip", "deflate", "br", "zstd"}
UPSTREAM_ERRORS = (TimeoutError, aiohttp.ClientError, OSError)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
DROP_REQUEST = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
DROP_RESPONSE = HOP_BY_HOP | {"content-length", "content-encoding"}


class ClientGone(Exception):
    """The downstream client closed its connection."""


class Proxy:
    def __init__(self, cfg: Config, store: Store, pricing: Pricing, budget: Budget) -> None:
        self.cfg = cfg
        self.store = store
        self.pricing = pricing
        self.budget = budget
        self.db = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skinflint-db")
        self.http: aiohttp.ClientSession | None = None
        self.diagnostics = cfg.cache_diagnostics
        self.last_message: OrderedDict[tuple[str, str | None], str] = OrderedDict()

    # -- lifecycle -------------------------------------------------------------------------

    async def start(self, app: web.Application) -> None:
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=None, sock_connect=30, sock_read=UPSTREAM_IDLE_TIMEOUT
            ),
            connector=aiohttp.TCPConnector(limit=0),
            auto_decompress=True,
            trust_env=True,
        )

    async def stop(self, app: web.Application) -> None:
        """Runs after every handler has finished: close upstream connections, then drain the
        ledger queue so the owner can close the store."""
        if self.http is not None:
            await self.http.close()
        await asyncio.to_thread(self.db.shutdown, True)

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app[PROXY] = self
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def run_db(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self.db, fn, *args)

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue a ledger write without waiting for it; failures are logged, never raised."""
        try:
            future = self.db.submit(fn, *args)
        except RuntimeError as e:
            log.error("ledger closed; dropped %s: %s", fn.__name__, e)
            return
        future.add_done_callback(log_failure)

    # -- request handling ------------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == HEALTH_PATH:
            return web.json_response(
                {
                    "ok": True,
                    "version": __version__,
                    "pid": os.getpid(),
                    "db": str(self.cfg.db_path),
                }
            )
        raw_path, _, raw_query = request.raw_path.partition("?")
        try:
            scope, target = split_scope(raw_path)
        except ValueError as e:
            return plain_error(400, str(e))
        scope = request.headers.get("x-skinflint-scope") or scope
        if not SCOPE_RE.match(scope):
            return plain_error(400, f"invalid scope {scope!r}: use 1-64 of A-Z a-z 0-9 . _ : @ + -")
        path = unquote(target)
        if raw_query:
            target += "?" + raw_query

        provider = detect(path, request.headers)
        adapter = ADAPTERS[provider]
        if request.headers.get("upgrade", "").lower() == "websocket":
            log.info("refused websocket upgrade on %s (clients fall back to HTTP)", path)
            return plain_error(426, "skinflint does not proxy WebSockets; use HTTP streaming")
        try:
            body = await request.read()
        except RequestPayloadError as e:
            # The parser is stuck mid-body: answer, then close the connection.
            request.content.feed_eof()
            encoding = request.headers.get("content-encoding", "none")
            log.warning("could not read the %s request body (%s): %r", path, encoding, e)
            resp = api_error(
                provider,
                400,
                f"skinflint: could not decode the request body (content-encoding: {encoding})",
                "invalid_request_error",
            )
            resp.force_close()
            return resp

        endpoint = adapter.endpoint(request.method, path)
        if endpoint is None:
            if (
                self.cfg.unmetered == "block"
                and self.cfg.budgets
                and request.method == "POST"
                and not path.startswith(FREE_POSTS)
            ):
                status, headers, err = adapter.error_body(
                    f'skinflint: {path} is not metered and [limits] unmetered = "block"'
                )
                return web.Response(status=status, headers=headers, body=err)
            return await self.passthrough(request, provider, target, body)
        return await self.metered(request, adapter, endpoint, scope, path, target, body)

    async def passthrough(
        self, request: web.Request, provider: Provider, target: str, body: bytes
    ) -> web.StreamResponse:
        try:
            upstream = await self.send(request, provider, target, body)
        except UPSTREAM_ERRORS as e:
            return self.upstream_failure(provider, e)
        async with upstream:
            resp = web.StreamResponse(status=upstream.status, headers=response_headers(upstream))
            try:
                await to_client(resp.prepare(request))
                async for chunk in upstream.content.iter_any():
                    await to_client(resp.write(chunk))
                await to_client(resp.write_eof())
            except ClientGone:
                upstream.close()
            except UPSTREAM_ERRORS as e:
                log.warning("upstream failed mid-response on %s: %s", target, e)
                drop_client(request)
            return resp

    async def metered(
        self,
        request: web.Request,
        adapter: Adapter,
        endpoint: Endpoint,
        scope: str,
        path: str,
        target: str,
        raw: bytes,
    ) -> web.StreamResponse:
        provider = adapter.provider
        started = time.time()
        try:
            data, info, segs, fp = await asyncio.to_thread(prepare, adapter, endpoint, raw)
        except Exception as e:  # noqa: BLE001 - unparseable bodies go upstream untouched
            log.warning("could not parse %s request body (%s); forwarding unmetered", path, e)
            return await self.passthrough(request, provider, target, raw)

        headers = request.headers
        session = headers.get("x-skinflint-session") or adapter.session_hint(headers, data)
        agent = headers.get("x-claude-code-agent-id") or ("fp:" + fp)
        record = Record(
            ts=started,
            provider=provider,
            endpoint=endpoint,
            scope=scope,
            model=info.model,
            state=State.PENDING,
            session=session,
            agent=agent,
            client=client_name(headers.get("user-agent")),
            stream=info.stream,
            plan="oauth-" in headers.get("anthropic-beta", ""),
            request_class=headers.get("x-claude-code-request-class"),
        )
        try:
            decision: Decision = await self.run_db(self.budget.admit, record, info)
        except Exception as e:  # noqa: BLE001
            log.error("could not check budgets for %s: %s", path, e)
            if self.cfg.budgets:
                return api_error(
                    provider, 503, f"skinflint: ledger unavailable, budgets not checked: {e}"
                )
            return await self.passthrough(request, provider, target, raw)
        for warning in decision.warnings:
            log.warning("%s", warning)
        if not decision.allowed:
            log.warning("BLOCKED %s %s %s: %s", scope, short(session), info.model, decision.message)
            status, err_headers, body = adapter.error_body(decision.message)
            return web.Response(status=status, headers=err_headers, body=body)
        assert decision.record_id is not None

        call = Call(
            self, request, record, decision.record_id, segs, raw if self.cfg.store_bodies else None
        )
        conversation = (session, agent) if session else None
        try:
            return await self.forward(
                call, adapter, endpoint, target, raw, data, info, conversation
            )
        except asyncio.CancelledError:
            call.settle(State.ABORTED, None, error="cancelled")
            raise
        except Exception as e:
            call.settle(State.ERROR, 500, error=f"proxy error: {e!r}")
            raise

    async def forward(
        self,
        call: Call,
        adapter: Adapter,
        endpoint: Endpoint,
        target: str,
        raw: bytes,
        data: dict[str, Any],
        info: RequestInfo,
        conversation: tuple[str, str | None] | None,
    ) -> web.StreamResponse:
        provider = adapter.provider
        request = call.request
        new, injected_usage, injected_diag = self.rewrite(
            adapter, endpoint, data, info, conversation
        )
        try:
            body = raw if new is None else await asyncio.to_thread(encode, new)
            upstream = await self.send(request, provider, target, body)
            if injected_diag and upstream.status == 400:
                async with upstream:
                    rejected = await upstream.read()
                if b"diagnostics" not in rejected:
                    return call.relay_bytes(upstream, rejected)
                log.warning("upstream rejected the diagnostics field; disabling cache diagnostics")
                self.diagnostics = False
                new, injected_usage, _ = self.rewrite(adapter, endpoint, data, info, None)
                body = raw if new is None else await asyncio.to_thread(encode, new)
                upstream = await self.send(request, provider, target, body)
            async with upstream:
                call.ttft = time.time() - call.record.ts
                limits = adapter.ratelimit_headers(upstream.headers)
                if limits:
                    self.submit(self.store.save_ratelimit, provider, limits, time.time())
                if upstream.status >= 300:
                    return call.relay_bytes(upstream, await upstream.read())
                if is_event_stream(upstream):
                    return await call.relay_stream(
                        upstream, adapter.stream_tracker(endpoint, injected_usage)
                    )
                return call.relay_json(upstream, await upstream.read(), endpoint)
        except UPSTREAM_ERRORS as e:
            call.settle(State.ERROR, 502, error=f"upstream unreachable: {e}")
            return self.upstream_failure(provider, e)

    def rewrite(
        self,
        adapter: Adapter,
        endpoint: Endpoint,
        data: dict[str, Any],
        info: RequestInfo,
        conversation: tuple[str, str | None] | None,
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        """Return (new body or None, injected stream usage, injected diagnostics)."""
        provider = adapter.provider
        previous: Any = ...
        if (
            self.diagnostics
            and conversation is not None
            and provider is Provider.ANTHROPIC
            and self.cfg.upstreams[provider] == FIRST_PARTY[provider]
            and "diagnostics" not in data
        ):
            previous = self.last_message.get(conversation)
        new = adapter.rewrite_request(
            endpoint,
            data,
            inject_stream_usage=self.cfg.inject_stream_usage,
            previous_message_id=previous,
        )
        if new is None:
            return None, False, False
        injected_usage = provider is Provider.OPENAI and info.stream
        injected_diag = "diagnostics" in new and "diagnostics" not in data
        return new, injected_usage, injected_diag

    def remember(self, conversation: tuple[str, str | None] | None, message_id: str | None) -> None:
        if conversation is None or not message_id:
            return
        self.last_message[conversation] = message_id
        self.last_message.move_to_end(conversation)
        while len(self.last_message) > MAX_CONVERSATIONS:
            self.last_message.popitem(last=False)

    async def send(
        self, request: web.Request, provider: Provider, target: str, body: bytes
    ) -> aiohttp.ClientResponse:
        """Send upstream; `target` is the raw (still percent-encoded) path and query."""
        assert self.http is not None
        return await self.http.request(
            request.method,
            URL(self.cfg.upstreams[provider] + target, encoded=True),
            headers=request_headers(request.headers),
            data=body if body else None,
            allow_redirects=False,
        )

    def upstream_failure(self, provider: Provider, error: BaseException) -> web.Response:
        log.error("upstream %s unreachable: %s", self.cfg.upstreams[provider], error)
        message = f"skinflint: could not reach {self.cfg.upstreams[provider]}: {error}"
        return api_error(provider, 502, message)


PROXY = web.AppKey("proxy", Proxy)


class Call:
    """One metered request in flight: relays the response and settles the ledger row once."""

    def __init__(
        self,
        proxy: Proxy,
        request: web.Request,
        record: Record,
        record_id: int,
        segs: list[Segment],
        body: bytes | None,
    ) -> None:
        self.proxy = proxy
        self.request = request
        self.record = record
        self.record_id = record_id
        self.segs = segs
        self.body = body
        self.ttft: float | None = None
        self.settled = False

    def relay_bytes(self, upstream: aiohttp.ClientResponse, data: bytes) -> web.Response:
        """A non-2xx upstream answer, passed through unchanged."""
        self.settle(State.ERROR, upstream.status, error=error_text(data.decode("utf-8", "replace")))
        return web.Response(status=upstream.status, headers=response_headers(upstream), body=data)

    def relay_json(
        self, upstream: aiohttp.ClientResponse, data: bytes, endpoint: Endpoint
    ) -> web.Response:
        adapter = ADAPTERS[self.record.provider]
        try:
            payload = json.loads(data)
            usage, model, upstream_id = adapter.usage_from_json(endpoint, payload)
            diag = adapter.cache_miss_from_json(payload)
            self.settle(
                State.OK,
                upstream.status,
                usage=usage,
                model=model,
                upstream_id=upstream_id,
                diag=diag,
            )
        except Exception as e:  # noqa: BLE001 - never fail the response because metering failed
            self.settle(State.OK, upstream.status, error=f"could not read usage: {e}")
        return web.Response(status=upstream.status, headers=response_headers(upstream), body=data)

    async def relay_stream(
        self, upstream: aiohttp.ClientResponse, tracker: StreamTracker
    ) -> web.StreamResponse:
        resp = web.StreamResponse(status=upstream.status, headers=response_headers(upstream))
        state = State.OK
        error: str | None = None
        metering = True
        try:
            await to_client(resp.prepare(self.request))
            async for chunk in upstream.content.iter_any():
                out = chunk
                if metering:
                    try:
                        out = tracker.feed(chunk)
                    except Exception as e:  # noqa: BLE001
                        metering, out, error = False, chunk, f"meter failed: {e}"
                if out:
                    await to_client(resp.write(out))
            if metering:
                tail = tracker.close()
                if tail:
                    await to_client(resp.write(tail))
            await to_client(resp.write_eof())
        except ClientGone:
            state, error = State.ABORTED, "client disconnected"
            upstream.close()
        except asyncio.CancelledError:
            upstream.close()
            self.settle_from(tracker, State.ABORTED, upstream.status, "cancelled")
            raise
        except UPSTREAM_ERRORS as e:
            state, error = State.ERROR, f"upstream stream failed: {e!r}"
            drop_client(self.request)
        if state is State.OK and not tracker.finished:
            state, error = State.ERROR, error or tracker.error or "stream ended early"
        elif state is State.OK and tracker.error:
            state, error = State.ERROR, tracker.error
        self.settle_from(tracker, state, upstream.status, error)
        return resp

    def settle_from(
        self, tracker: StreamTracker, state: State, status: int, error: str | None
    ) -> None:
        diag = (
            getattr(tracker, "cache_miss_reason", None),
            getattr(tracker, "cache_missed_tokens", None),
        )
        self.settle(
            state,
            status,
            usage=tracker.usage,
            model=tracker.model,
            upstream_id=tracker.upstream_id,
            error=error,
            diag=diag,
        )

    def settle(
        self,
        state: State,
        status: int | None,
        *,
        usage: Usage | None = None,
        model: str | None = None,
        upstream_id: str | None = None,
        error: str | None = None,
        diag: tuple[str | None, int | None] = (None, None),
    ) -> None:
        """Compute cost and queue the ledger update. Synchronous so cancellation cannot skip it."""
        if self.settled:
            return
        self.settled = True
        proxy, rec = self.proxy, self.record
        rec.state = state
        rec.status = status
        rec.usage = usage or Usage()
        if model:
            rec.model = model
        rec.upstream_id = upstream_id
        rec.error = error
        rec.cache_miss_reason, rec.cache_missed_tokens = diag
        rec.duration_ms = int((time.time() - rec.ts) * 1000)
        rec.ttft_ms = int(self.ttft * 1000) if self.ttft is not None else None
        try:
            rec.cost_usd, rec.cost_estimated = proxy.pricing.cost(
                rec.provider, rec.model, rec.usage
            )
        except Exception as e:  # noqa: BLE001
            rec.cost_usd, rec.cost_estimated = 0.0, True
            log.warning("could not price %s: %s", rec.model, e)
        if rec.usage.prompt_tokens:
            segments.calibrate(self.segs, rec.usage.prompt_tokens)
        rec.reserved_usd = 0.0
        rec.reserved_tokens = 0
        if state is State.OK and rec.session:
            proxy.remember((rec.session, rec.agent), upstream_id)
        proxy.submit(proxy.store.finish, self.record_id, rec, self.segs, self.body)
        log_record(rec)


# -- helpers -------------------------------------------------------------------------------


def prepare(
    adapter: Adapter, endpoint: Endpoint, raw: bytes
) -> tuple[dict[str, Any], RequestInfo, list[Segment], str]:
    """Parse and profile a metered request body (CPU-bound; runs off the event loop)."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("body is not a JSON object")
    info = adapter.parse_request(endpoint, data)
    segs = segments.segment(adapter.provider, endpoint, data)
    return data, info, segs, segments.fingerprint(segs)


def encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


async def to_client(aw: Awaitable[T]) -> T:
    """Await a write to the downstream client; a closed connection raises ClientGone."""
    try:
        return await aw
    except ConnectionError as e:
        raise ClientGone(str(e)) from e


def drop_client(request: web.Request) -> None:
    """Close the client connection without finishing the response, so a truncated upstream
    answer reaches the client as a truncated answer rather than a clean end of stream."""
    if request.transport is not None:
        request.transport.close()


def log_failure(future: Future) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.error("ledger write failed: %s", future.exception())


def split_scope(path: str) -> tuple[str, str]:
    """'/s/<scope>/v1/messages' -> ('<scope>', '/v1/messages'); otherwise ('default', path)."""
    if not path.startswith("/s/"):
        return "default", path
    rest = path[3:]
    scope, sep, tail = rest.partition("/")
    if not scope:
        raise ValueError("empty scope in /s/<scope>/ path")
    return unquote(scope), "/" + tail if sep else "/"


def request_headers(headers: Mapping[str, str]) -> CIMultiDict[str]:
    out: CIMultiDict[str] = CIMultiDict()
    for key, value in headers.items():
        lower = key.lower()
        if lower in DROP_REQUEST or lower.startswith("x-skinflint-"):
            continue
        if lower == "content-encoding" and value.isascii() and value.lower() in DECODED_ENCODINGS:
            continue  # aiohttp decoded the body; we forward it decoded
        out.add(key, value)
    return out


def response_headers(upstream: aiohttp.ClientResponse) -> CIMultiDict[str]:
    out: CIMultiDict[str] = CIMultiDict()
    for key, value in upstream.headers.items():
        if key.lower() not in DROP_RESPONSE:
            out.add(key, value)
    return out


def is_event_stream(upstream: aiohttp.ClientResponse) -> bool:
    return upstream.headers.get("content-type", "").startswith("text/event-stream")


def plain_error(status: int, message: str) -> web.Response:
    return web.json_response(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": f"skinflint: {message}"},
        },
        status=status,
        headers={"x-should-retry": "false"},
    )


def api_error(
    provider: Provider, status: int, message: str, kind: str | None = None
) -> web.Response:
    """An error in the provider's own shape (for failures that are not budget refusals)."""
    if provider is Provider.ANTHROPIC:
        body: dict[str, Any] = {
            "type": "error",
            "error": {"type": kind or "api_error", "message": message},
        }
    else:
        body = {
            "error": {
                "message": message,
                "type": kind or "server_error",
                "param": None,
                "code": None,
            }
        }
    return web.json_response(body, status=status)


def error_text(text: str) -> str:
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:500]
    except ValueError:
        pass
    return text.strip()[:500]


def short(session: str | None) -> str:
    return session[:8] if session else "-"


def log_record(rec: Record) -> None:
    from skinflint import fmt

    u = rec.usage
    hit = u.cache_hit_rate
    parts = [
        rec.scope,
        short(rec.session),
        rec.model,
        f"in {fmt.tokens(u.prompt_tokens)}"
        + (f" ({fmt.percent(hit)} cached)" if hit is not None else ""),
        f"out {fmt.tokens(u.output_tokens)}",
        fmt.money(rec.cost_usd) + ("~" if rec.cost_estimated else ""),
        fmt.duration((rec.duration_ms or 0) / 1000),
    ]
    if rec.cache_miss_reason:
        parts.append(f"cache miss: {rec.cache_miss_reason}")
    if rec.state is not State.OK:
        parts.append(f"{rec.state.value.upper()} {rec.status or ''} {rec.error or ''}".strip())
    log.info("  ".join(parts))


# -- running -------------------------------------------------------------------------------


def create_app(cfg: Config, store: Store, pricing: Pricing, budget: Budget) -> web.Application:
    """The proxy as an aiohttp application; the caller owns (and closes) the store."""
    return Proxy(cfg, store, pricing, budget).app()


def build(cfg: Config) -> tuple[Proxy, Store]:
    store = Store(cfg.db_path)
    store.mark_lost(time.time() - PENDING_TTL)
    pricing = Pricing.load(cfg.prices, unknown_model=cfg.unknown_model)
    budget = Budget(cfg.budgets, store, pricing, reserve=cfg.reserve, config_path=cfg.source)
    return Proxy(cfg, store, pricing, budget), store


async def _serve(
    cfg: Config, ready: threading.Event | None = None, holder: dict | None = None
) -> None:
    proxy, store = build(cfg)
    try:
        runner = web.AppRunner(proxy.app(), access_log=None, handle_signals=False)
        await runner.setup()
        try:
            site = web.TCPSite(runner, cfg.host, cfg.port, reuse_address=True)
            await site.start()
            port = runner.addresses[0][1] if runner.addresses else cfg.port
            stop = asyncio.Event()
            if holder is not None:
                holder.update(port=port, stop=stop, loop=asyncio.get_running_loop())
            if ready is not None:
                ready.set()
            await stop.wait()
        finally:
            await runner.cleanup()
    finally:
        store.close()


def serve(cfg: Config) -> None:
    """Run the proxy in the foreground until interrupted."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(cfg))


@contextlib.contextmanager
def embedded(cfg: Config) -> Iterator[str]:
    """Run a proxy on a background thread; yield its base URL (``http://127.0.0.1:<port>``)."""
    ready = threading.Event()
    holder: dict[str, Any] = {}
    errors: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(_serve(cfg, ready, holder))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            ready.set()

    thread = threading.Thread(target=target, name="skinflint-proxy", daemon=True)
    thread.start()
    ready.wait()
    if errors:
        thread.join(timeout=10)
        raise errors[0]
    try:
        yield f"http://{cfg.host}:{holder['port']}"
    finally:
        holder["loop"].call_soon_threadsafe(holder["stop"].set)
        thread.join(timeout=10)
        if errors:
            raise errors[0]
