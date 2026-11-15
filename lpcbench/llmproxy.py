"""Local proxy between pi and an OpenAI-compatible provider.

pi has no sampling flags; it sends each model's `samplingParams` from
models.json, and the proxy lays `--sampling` overrides on top. The proxy also
keeps provider credentials out of the sandbox, re-sends a request the backend
never starts answering, and measures each session's prompt-cache reuse and
first-byte latency. Routing is the router's job: pi's own `x-session-affinity`
header passes through unchanged for cache locality.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import select
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "authorization", "accept-encoding"}
AFFINITY = "x-session-affinity"
BACKEND = "x-llama-backend"    # which server answered; only on non-streamed responses
_CACHE_N = re.compile(rb'"cache_n":(\d+)')
# The router names the server in an SSE comment (which pi skips) once it has picked one;
# ": queued" keepalive comments may come first while the request waits.
_SSE_BACKEND = re.compile(rb'^: x-llama-backend (\S+)', re.M)
_PROMPT = re.compile(rb'"prompt_tokens":(\d+)')
UPSTREAM_TIMEOUT_S = 900
# Slowest healthy first token seen: 20.4 s over 396 qwen trials, 30.3 s over DS loops.
FIRST_BYTE_S = float(os.environ.get("LPCBENCH_FIRST_BYTE_S", "90"))
FIRST_BYTE_RETRIES = 2
# Gateway answers sent before any model output (router restarting, Cloudflare): sent again.
RETRY_STATUS = {502, 503, 504, 520, 521, 522, 523, 524}
RETRY_PAUSE_S = 2.0


class NoAnswer(http.client.HTTPException):
    """The upstream stream ended, or answered with a gateway error, before any data."""


class ClientGone(Exception):
    """pi closed its connection (the trial was cut off) while the request waited upstream."""


def client_gone(sock: Any) -> bool:
    """True once the client has closed its side. pi sends one request per connection
    and then only reads, so a readable socket with nothing to read is a closed one."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def only_comments(head: bytes) -> bool:
    """True while a streamed body holds nothing but SSE comments (the router's
    keepalives and backend name): no model output has started yet."""
    return all(not ln.strip() or ln.startswith(b":") for ln in head.split(b"\n"))


def rewrite_body(body: bytes, sampling: Dict[str, Any]) -> bytes:
    """Lay `sampling` over what pi sends."""
    req = json.loads(body)
    req.update(sampling)
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def new_stats() -> Dict[str, Any]:
    return {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0, "last_prompt": 0, "cache_misses": 0,
            "max_first_byte_s": 0.0, "first_byte_retries": 0}


class LLMProxy:
    def __init__(self, provider: Dict[str, Any], provider_name: str,
                 sampling: Optional[Dict[str, Any]] = None):
        self.provider, self.provider_name = provider, provider_name
        self.sampling = dict(sampling or {})
        self.upstream = urlsplit(provider["baseUrl"].rstrip("/"))
        self.requests = 0
        self._lock = threading.Lock()
        self.stats: Dict[str, Dict[str, Any]] = {}
        self.busy_s: Dict[str, float] = {}     # backend -> seconds with a request in flight
        self.started = time.monotonic()
        # One JSON line per upstream request (session, backend, first byte, status),
        # so a stalled or retried turn can be examined after the run.
        self.log_path: Optional[Path] = None
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                proxy.forward(self)

            def do_GET(self) -> None:
                proxy.forward(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def session_url(self, session: str, base_url: Optional[str] = None) -> str:
        return f"{base_url or f'http://127.0.0.1:{self.port}'}/s/{session}/v1"

    def install(self, home_cfg: Path, session: Optional[str] = None, base_url: Optional[str] = None) -> str:
        """Point the sandbox's models.json at the proxy, without credentials.
        `base_url`: where the proxy is from inside the jail (a relayed loopback port)."""
        session = session or uuid.uuid4().hex[:16]
        with self._lock:
            self.stats[session] = new_stats()
        path = home_cfg / "models.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        entry = cfg["providers"][self.provider_name]
        entry["baseUrl"] = self.session_url(session, base_url)
        entry["apiKey"] = "local-proxy"
        entry.pop("headers", None)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return session

    def release(self, session: str) -> Dict[str, Any]:
        """The session's stats, and forget it."""
        with self._lock:
            return dict(self.stats.pop(session, {}))

    def utilization(self) -> Dict[str, float]:
        """backend -> fraction of the proxy's lifetime with a request in flight there."""
        wall = max(1e-9, time.monotonic() - self.started)
        with self._lock:
            return {b: round(s / wall, 3) for b, s in sorted(self.busy_s.items())}

    def forward(self, h: BaseHTTPRequestHandler) -> None:
        parts = h.path.split("/", 3)  # "", "s", session, rest
        if len(parts) < 4 or parts[1] != "s":
            h.send_error(404)
            return
        session, rest = parts[2], parts[3]
        content_len = int(h.headers.get("Content-Length") or 0)
        if content_len > 4 * 1024 * 1024:
            h.send_error(413, "Payload Too Large")
            return
        body = h.rfile.read(content_len)
        if h.command == "POST" and rest.endswith("chat/completions"):
            body = rewrite_body(body, self.sampling)
            with self._lock:
                self.requests += 1
        # baseUrl ends in /v1 and so does `rest`.
        target = self.upstream.path.rsplit("/v1", 1)[0] + "/" + rest
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP_HEADERS}
        # pi sends its own affinity id; a client that does not still gets one
        # stable id per session, so the router can keep the slot's cache warm.
        if not any(k.lower() == AFFINITY for k in headers):
            headers[AFFINITY] = session
        headers.update(self.provider.get("headers") or {})
        headers["Authorization"] = f"Bearer {self.provider.get('apiKey', '')}"
        headers["Content-Length"] = str(len(body))
        t0 = time.monotonic()
        served, conn, resp, chunk, started = "", None, None, b"", False
        try:
            # A request the backend never starts answering is sent again here:
            # nothing has reached pi yet, so pi never notices and the trial keeps its
            # work. The router sends headers early and ": queued" comments while a
            # request waits, so wait for data: a stream that drops or ends before
            # any (304 of them while the router crashed on 2026-11-11), or a gateway
            # error status, is sent again too. pi would otherwise see a failed turn.
            # While it waits, the proxy writes nothing to pi, so it would not notice pi
            # going away: a trial cut off by the stall limit left its request queued at
            # the router for the full UPSTREAM_TIMEOUT_S (LB3 round 4, 2026-11-12). The
            # client is checked between keepalives, and the request dropped once it is gone.
            for attempt in range(FIRST_BYTE_RETRIES + 1):
                if client_gone(h.connection):
                    raise ClientGone()
                attempt_t0 = time.monotonic()
                conn = self._connect(FIRST_BYTE_S)
                try:
                    conn.request(h.command, target, body, headers)
                    resp = conn.getresponse()
                    last = attempt == FIRST_BYTE_RETRIES
                    if resp.status in RETRY_STATUS and not last:
                        raise NoAnswer(f"status {resp.status}")   # the last one goes to pi as it is
                    chunk = resp.read1(65536)
                    while chunk and resp.status == 200 and only_comments(chunk):
                        # Per attempt: timed from the first attempt, a re-send after this
                        # limit failed at once (both retries within a second).
                        if time.monotonic() - attempt_t0 > UPSTREAM_TIMEOUT_S:
                            raise TimeoutError("no data")
                        if client_gone(h.connection):
                            raise ClientGone()
                        more = resp.read1(65536)
                        if not more:
                            raise NoAnswer("stream ended before any data")
                        chunk += more
                    break
                except (TimeoutError, OSError, http.client.HTTPException) as exc:
                    conn.close()
                    conn = resp = None
                    if attempt == FIRST_BYTE_RETRIES:
                        raise
                    self._log({"t": round(time.time(), 1), "session": session, "retry": attempt + 1,
                               "error": type(exc).__name__, "detail": str(exc)[:80],
                               "after_s": round(time.monotonic() - t0, 1)})
                    with self._lock:
                        st = self.stats.get(session)
                        if st is not None:
                            st["first_byte_retries"] += 1
                    if isinstance(exc, NoAnswer):
                        time.sleep(RETRY_PAUSE_S)
            first = time.monotonic() - t0
            if conn.sock is not None:
                conn.sock.settimeout(UPSTREAM_TIMEOUT_S)
            served = resp.getheader(BACKEND) or ""
            if not served:
                m = _SSE_BACKEND.search(chunk)
                served = m.group(1).decode("ascii", "replace") if m else ""
            self._log({"t": round(time.time(), 1), "session": session, "backend": served, "status": resp.status,
                       "first_byte_s": round(first, 1), "attempt": attempt + 1, "bytes": len(body)})
            h.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    h.send_header(k, v)
            h.send_header("Transfer-Encoding", "chunked")
            h.send_header("Connection", "close")
            h.end_headers()
            h.close_connection = True
            started = True
            tail = b""
            while chunk:
                tail = (tail + chunk)[-8192:]
                h.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                h.wfile.flush()
                chunk = resp.read1(65536)
            # Count the turn before pi sees the end of the stream: pi may exit right
            # after its last answer, and the trial's stats are read when it does.
            if h.command == "POST":
                self._note_usage(session, served, tail, first, time.monotonic() - t0, resp.status)
            h.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the trial was cut off; closing upstream cancels the generation
        except ClientGone:
            h.close_connection = True
            self._log({"t": round(time.time(), 1), "session": session, "backend": served, "status": 499,
                       "error": "ClientGone", "after_s": round(time.monotonic() - t0, 1)})
        except (OSError, http.client.HTTPException) as exc:
            h.close_connection = True
            # Once pi has the status line, the body is a chunked stream: an error page
            # written now would land inside it as garbage. Ending the connection without
            # the last chunk is what pi reads as a cut stream ("terminated").
            self._log({"t": round(time.time(), 1), "session": session, "backend": served,
                       "status": resp.status if started and resp is not None else 502, "midstream": started,
                       "error": type(exc).__name__, "after_s": round(time.monotonic() - t0, 1)})
            if not started:
                try:
                    h.send_error(502, type(exc).__name__)
                except OSError:
                    pass
        finally:
            if conn is not None:
                conn.close()
            if h.command == "POST" and served:
                with self._lock:
                    self.busy_s[served] = self.busy_s.get(served, 0.0) + time.monotonic() - t0

    def _log(self, row: Dict[str, Any]) -> None:
        if self.log_path is None:
            return
        with self._lock:
            try:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            except OSError:
                pass

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        return cls(self.upstream.netloc, timeout=timeout)

    def _note_usage(self, session: str, served: str, tail: bytes, first_s: float = 0.0,
                    total_s: float = 0.0, status: int = 200) -> None:
        """Prompt-cache health: on a warm slot a turn reuses nearly all of the last
        prompt; a turn that reuses under half of it (`cache_misses`) was
        reprocessed, which costs time but, since the server's cache fix, nothing else.
        A gateway error passed on to pi (every attempt failed) is logged but is not a
        model request: pi counts it as a failed turn (`error_turns`)."""
        cache = _CACHE_N.findall(tail)
        prompt = _PROMPT.findall(tail)
        self._log({"t": round(time.time(), 1), "session": session, "backend": served, "done": True,
                   "status": status, "first_byte_s": round(first_s, 1), "total_s": round(total_s, 1),
                   "prompt_n": int(prompt[-1]) if prompt else None, "cache_n": int(cache[-1]) if cache else None})
        with self._lock:
            st = self.stats.get(session)
            if st is None or status != 200:
                return
            st["requests"] += 1
            st["max_first_byte_s"] = round(max(st["max_first_byte_s"], first_s), 1)
            if not prompt:
                return
            p, c = int(prompt[-1]), int(cache[-1]) if cache else 0
            st["cache_misses"] += int(st["last_prompt"] > 0 and c < st["last_prompt"] // 2)
            st["prompt_tokens"] += p
            st["cached_tokens"] += c
            st["last_prompt"] = p


def _router_view(url: str, headers: Dict[str, str], timeout: float) -> Dict[str, int]:
    """backend -> total_slots from a router's /proxy/slots at `url`; {} if it does not answer one."""
    up = urlsplit(url)
    cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
    conn = cls(up.netloc, timeout=timeout)
    try:
        conn.request("GET", up.path or "/proxy/slots", headers={**headers, "User-Agent": "lpcbench"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            return {}
        view = json.loads(raw)
        return {b: int(v.get("total_slots") or 0) for b, v in (view.get("backends") or {}).items()
                if v.get("up", True)}
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return {}
    finally:
        conn.close()


def cluster_slots(provider: Dict[str, Any], probes: int = 8, timeout: float = 20.0) -> Dict[str, int]:
    """backend -> total_slots. First from the router's GET /proxy/slots (its own view
    of every node): at LPCBENCH_SLOTS_URL when set (the public router route has no
    location for /proxy/slots and answers 302, so point this at the router itself, e.g.
    through an ssh tunnel), else next to the provider's /v1. Else from llama.cpp's /props
    through the router under a few affinity ids (the router names the server in
    x-llama-backend; an idle router sends them all to one node, so this is a floor).
    The sum is the number of generations the cluster runs at once. Empty when neither answers."""
    up = urlsplit(provider["baseUrl"].rstrip("/"))
    cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
    root = up.path.rsplit("/v1", 1)[0]
    hdrs = dict(provider.get("headers") or {})
    for url in (os.environ.get("LPCBENCH_SLOTS_URL", ""), f"{up.scheme}://{up.netloc}{root}/proxy/slots"):
        # the provider's credentials go to the provider's host only
        found = _router_view(url, hdrs if urlsplit(url).netloc == up.netloc else {}, timeout) if url else {}
        if found:
            return found
    path = root + "/props"
    found = {}
    for k in range(probes):
        conn = cls(up.netloc, timeout=timeout)
        headers = {**(provider.get("headers") or {}), "Authorization": f"Bearer {provider.get('apiKey', '')}",
                   "User-Agent": "lpcbench", AFFINITY: f"lpcb-props-{k}"}
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                return {}
            slots = int(json.loads(raw).get("total_slots") or 0)
            backend = resp.getheader(BACKEND) or ""
        except (OSError, ValueError, http.client.HTTPException):
            return {}
        finally:
            conn.close()
        if not backend:
            return {"": slots} if slots else {}     # a single server, or a router that names none
        found[backend] = slots
    return found


def pi_sampling(model: str = "self") -> Dict[str, Any]:
    """The samplingParams pi sends for `model` with thinking off ("self" is the
    first model of the first provider, pi's default), so a run records what
    the server was actually asked for."""
    try:
        cfg = json.loads((Path.home() / ".pi/agent/models.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = [m for p in cfg.get("providers", {}).values() for m in p.get("models", [])]
    if not models:
        return {}
    pick = models[0] if model in ("self", "default", "") else next(
        (m for m in models if m.get("id") == model.split("/")[-1]), {})
    return dict(pick.get("samplingParams") or {})


def from_pi_config(sampling: Optional[Dict[str, Any]] = None) -> LLMProxy:
    """A proxy for the first provider in the user's pi models.json."""
    cfg = json.loads((Path.home() / ".pi/agent/models.json").read_text(encoding="utf-8"))
    name, provider = next(iter(cfg["providers"].items()))
    return LLMProxy(provider, name, sampling)


def pi_cluster_slots() -> Dict[str, int]:
    cfg = json.loads((Path.home() / ".pi/agent/models.json").read_text(encoding="utf-8"))
    provider = next(iter(cfg["providers"].values()))
    return cluster_slots(provider)
