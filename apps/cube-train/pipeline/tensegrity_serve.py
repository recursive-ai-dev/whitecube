#!/usr/bin/env python3
"""tensegrity_serve - OpenAI-compatible server for checkpoints in ~/train-out.

Lets any OpenAI-style client (cube-chat, curl, scripts) talk to the local
Tensegrity checkpoints:

  tg-serve                       # http://127.0.0.1:8791  (CPU)
  tg-serve --device cuda         # same, on the GPU
  tg-serve --port 8800 --verbose

Endpoints:
  GET  /health                  -> {"ok": true, "runs": N}
  GET  /v1/models               -> one entry per run in ~/train-out
  POST /v1/chat/completions     -> SSE stream, or plain JSON when stream=false

Notes that matter for this model:
  * Context is cfg.block_size (256) BYTES, so the prompt is trimmed from the
    left and the newest text wins.
  * max_tokens is capped at block_size // 2, which keeps prompt + reply inside
    one window and avoids a KV-cache rebuild on every decode step.
  * System messages are dropped by default: cube-chat ships a ~700-byte system
    prompt that would evict the whole window. --format labels keeps them.
  * The corpus is plain prose, so the default format is plain continuation.

Stdlib + torch only. An OpenAI `tools` field in a request is ignored.
"""
import argparse
import codecs
import json
import math
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import torch  # noqa: E402

from tensegrity_lm import ByteTokenizer, TensegrityConfig, TensegrityLM, load_checkpoint  # noqa: E402

DEFAULT_PORT = 8791
EOS = ByteTokenizer.EOS


# --------------------------------------------------------------------------
# runs on disk
# --------------------------------------------------------------------------

def discover_runs(runs_dir: Path, ckpt_name: str):
    """Newest-first list of runs that have the requested checkpoint file."""
    runs = []
    if not runs_dir.is_dir():
        return runs
    for entry in runs_dir.iterdir():
        ckpt = entry / ckpt_name
        if not entry.is_dir() or not ckpt.is_file():
            continue
        info = {"id": entry.name, "checkpoint": str(ckpt), "step": None,
                "val_loss": None, "parameters": None, "mtime": ckpt.stat().st_mtime}
        for name in ("result.json", "run.json"):
            try:
                with open(entry / name) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            info["step"] = data.get("completed_steps", data.get("steps", info["step"]))
            info["val_loss"] = data.get("best_val_loss", info["val_loss"])
            info["parameters"] = data.get("parameters", info["parameters"])
        runs.append(info)
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


# --------------------------------------------------------------------------
# model cache
# --------------------------------------------------------------------------

class ModelStore:
    """Loads checkpoints on demand, then keeps them warm."""

    def __init__(self, runs_dir: Path, device: str, ckpt_name: str, defaults: dict):
        self.runs_dir = runs_dir
        self.device = device
        self.ckpt_name = ckpt_name
        self.defaults = defaults
        self.gen_lock = threading.Lock()   # serializes generation
        self._cache = {}
        self._load_lock = threading.Lock()

    def runs(self):
        return discover_runs(self.runs_dir, self.ckpt_name)

    def resolve(self, name):
        """Map a requested model id to a run; 'latest' (or empty) is the newest."""
        runs = self.runs()
        if not runs:
            raise LookupError(
                f"no {self.ckpt_name} in {self.runs_dir} - train one first")
        if not name or name == "latest":
            return runs[0]
        for run in runs:
            if run["id"] == name:
                return run
        matches = [r for r in runs if r["id"].startswith(name)]
        if len(matches) == 1:
            return matches[0]
        raise LookupError(
            f"unknown model {name!r}; try one of: "
            + ", ".join(r["id"] for r in runs))

    def get(self, name):
        """-> (run_id, model, cfg, step, val_loss), loading the checkpoint if needed."""
        run = self.resolve(name)
        key = run["id"]
        with self._load_lock:
            hit = self._cache.get(key)
            if hit is None:
                hit = self._load(run)
                self._cache[key] = hit
        model, cfg, step, val_loss = hit
        return key, model, cfg, step, val_loss

    def _load(self, run):
        t0 = time.time()
        ckpt = load_checkpoint(run["checkpoint"], "cpu")
        cfg = TensegrityConfig(**ckpt["config"])
        model = TensegrityLM(cfg)
        model.load_state_dict(ckpt["model_state_dict"])
        model.to(self.device)
        model.eval()
        loaded = (model, cfg, ckpt.get("step"), ckpt.get("best_val_loss"))
        print(f"[serve] loaded {run['id']} step={ckpt.get('step')} "
              f"val={ckpt.get('best_val_loss')} on {self.device} "
              f"in {time.time() - t0:.2f}s", flush=True)
        return loaded


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------

def _content_of(message):
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def build_prompt(messages, budget: int, fmt: str):
    """-> (token ids, prompt text) keeping the newest `budget` bytes."""
    parts = []
    for message in messages or []:
        role = message.get("role") or "user"
        text = _content_of(message).strip()
        if not text:
            continue
        if role == "system":
            if fmt == "labels":
                parts.append(("system", text))
            continue  # raw: system text would only evict the window
        if role == "tool":
            if fmt == "labels":
                parts.append(("tool", text))
            continue
        parts.append((role, text))
    if not parts:
        return [], ""

    if fmt == "labels":
        body = "\n\n".join(f"{role}: {text}" for role, text in parts) + "\nassistant:"
    else:
        body = "\n\n".join(text for _, text in parts)

    raw = body.encode("utf-8")
    if len(raw) > budget:
        raw = b" " + raw[-budget:]
        while len(raw) > 1 and (raw[1] & 0xC0) == 0x80:  # no mid-codepoint start
            raw = raw[1:]
    return list(raw), raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

def sample(logits, temperature, top_k, top_p):
    """One step of the same sampling stack TensegrityLM.generate() uses."""
    if not math.isfinite(temperature) or temperature < 0:
        raise ValueError('temperature must be finite and nonnegative')
    if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1):
        raise ValueError('top_k must be a positive integer or None')
    if top_p is not None and (not math.isfinite(top_p) or not 0 < top_p <= 1):
        raise ValueError('top_p must be within (0, 1] or None')
    logits = logits.float()
    if torch.isnan(logits).any() or torch.isposinf(logits).any() or not torch.isfinite(logits).any():
        raise RuntimeError('Cannot sample nonfinite model logits')
    if temperature <= 0.0:
        return int(logits.argmax(dim=-1).item())
    logits = logits / temperature
    if top_k:
        k = min(top_k, logits.size(-1))
        threshold = torch.topk(logits, k).values[..., -1, None]
        logits = logits.masked_fill(logits < threshold, -float("inf"))
    if top_p and 0.0 < top_p < 1.0:
        sorted_logits, sorted_idx = torch.sort(logits, descending=True, dim=-1)
        probs = torch.softmax(sorted_logits, dim=-1)
        cumprobs = torch.cumsum(probs, dim=-1)
        cutoff = cumprobs > top_p
        cutoff[..., 1:] = cutoff[..., :-1].clone()
        cutoff[..., 0] = False
        sorted_logits = sorted_logits.masked_fill(cutoff, -float("inf"))
        logits = torch.full_like(logits, -float("inf")).scatter(-1, sorted_idx,
                                                                sorted_logits)
    probs = torch.softmax(logits, dim=-1)
    return int(torch.multinomial(probs, num_samples=1).item())


@torch.no_grad()
def decode(model, prompt_ids, *, temperature, top_k, top_p, max_new_tokens):
    """Yield sampled token ids, reusing the KV cache exactly like generate().

    EOS is yielded too - callers decide when to stop, so the stop reason is
    reported accurately.
    """
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens < 0:
        raise ValueError('max_new_tokens must be a nonnegative integer')
    prompt_ids = list(prompt_ids) or [ByteTokenizer.BOS]
    if any(isinstance(token, bool) or not isinstance(token, int) or not 0 <= token < model.cfg.vocab_size
           for token in prompt_ids):
        raise ValueError('Prompt token IDs must be integers within the model vocabulary')
    device = next(model.parameters()).device
    seq = torch.tensor([prompt_ids], dtype=torch.long, device=device)
    past = None
    for _ in range(max_new_tokens):
        length = seq.size(1)
        if past is None or length > model.cfg.block_size:
            window = seq if length <= model.cfg.block_size else seq[:, -model.cfg.block_size:]
            logits, past, _ = model.forward_incremental(window, None, 0)
        else:
            logits, past, _ = model.forward_incremental(seq[:, -1:], past, length - 1)
        token = sample(logits[:, -1, :], temperature, top_k, top_p)
        yield token
        if token == EOS:
            return
        # Special tokens are always fed back in: skipping them would re-feed the
        # same position forever. ByteTokenizer.decode drops them from the text.
        seq = torch.cat([seq, torch.tensor([[token]], dtype=torch.long,
                                           device=device)], dim=1)


def _clean(text: str) -> str:
    """Drop control characters the corpus never contains; keep \\n and \\t."""
    return "".join(ch for ch in text if ch >= " " or ch in "\n\t")


class StreamDecoder:
    """Incremental UTF-8 decoding that never splits a codepoint across chunks."""

    def __init__(self):
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, token_ids):
        if not token_ids:
            return ""
        raw = bytes(i for i in token_ids if 0 <= i <= 255)
        return _clean(self._dec.decode(raw)) if raw else ""

    def flush(self):
        return _clean(self._dec.decode(b"", final=True))


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TensegrityServe/1.0"
    protocol_version = "HTTP/1.0"  # close per response; keeps SSE framing simple
    store: ModelStore = None

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.store and self.store.defaults.get("verbose"):
            sys.stderr.write("[serve] %s - %s\n"
                             % (self.address_string(), fmt % args))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message, err_type="invalid_request_error"):
        self._send_json({"error": {"message": message, "type": err_type,
                                   "code": None, "param": None}}, status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/health", "/v1/health"):
            self._send_json({"ok": True, "runs": len(self.store.runs())})
        elif path == "/v1/models":
            self._send_json({
                "object": "list",
                "data": [{"id": r["id"], "object": "model", "created": int(r["mtime"]),
                          "owned_by": "train-out", "step": r["step"],
                          "val_loss": r["val_loss"], "parameters": r["parameters"]}
                         for r in self.store.runs()],
            })
        elif path in ("/", "/v1"):
            self._send_json({
                "service": "tensegrity-serve",
                "runs_dir": str(self.store.runs_dir),
                "device": self.store.device,
                "models": [r["id"] for r in self.store.runs()],
                "endpoints": ["/health", "/v1/models", "POST /v1/chat/completions"],
            })
        else:
            self._send_error(404, f"no such path: {self.path}", "not_found")

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/v1/chat/completions":
            self._send_error(404, f"no such path: {self.path}", "not_found")
            return
        try:
            request = self._read_body()
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_error(400, f"invalid JSON body: {exc}")
            return
        try:
            self.handle_chat(request)
        except LookupError as exc:
            self._send_error(404, str(exc), "model_not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream
        except Exception as exc:  # never leave the client hanging
            try:
                self._send_error(500, f"{type(exc).__name__}: {exc}", "server_error")
            except OSError:
                pass
            print(f"[serve] error: {exc!r}", flush=True)

    def handle_chat(self, request):
        store = self.store
        name, model, cfg, step, val_loss = store.get(request.get("model"))
        d = store.defaults
        temperature = float(request.get("temperature", d["temperature"]))
        top_k = request.get("top_k", d["top_k"])
        top_k = int(top_k) if top_k else None
        top_p = request.get("top_p")
        top_p = float(top_p) if top_p else None
        want = int(request.get("max_tokens") or request.get("max_completion_tokens")
                   or d["max_tokens"])
        max_new = max(1, min(want, cfg.block_size // 2))
        fmt = request.get("format") or d["format"]
        stop = _as_list(request.get("stop"))
        stream = bool(request.get("stream", False))

        prompt_ids, _ = build_prompt(request.get("messages"),
                                     max(cfg.block_size - max_new, 64), fmt)
        if not prompt_ids:
            self._send_error(400, "messages must contain at least some text")
            return

        meta = {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "created": int(time.time()),
                "model": name, "step": step, "val_loss": val_loss,
                "prompt_bytes": len(prompt_ids), "max_tokens": max_new}

        if not stream:
            text, used, finish = self._generate(model, meta, prompt_ids, temperature,
                                                top_k, top_p, max_new, stop, emit=None)
            self._send_json({
                "id": meta["id"], "object": "chat.completion",
                "created": meta["created"], "model": name,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt_ids), "completion_tokens": used,
                          "total_tokens": len(prompt_ids) + used},
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(payload):
            self.wfile.write(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        try:
            emit({"id": meta["id"], "object": "chat.completion.chunk",
                  "created": meta["created"], "model": name,
                  "choices": [{"index": 0,
                               "delta": {"role": "assistant", "content": ""},
                               "finish_reason": None}],
                  "step": step, "val_loss": val_loss,
                  "prompt_bytes": len(prompt_ids)})
            _, _, finish = self._generate(model, meta, prompt_ids, temperature, top_k,
                                          top_p, max_new, stop, emit=emit)
            emit({"id": meta["id"], "object": "chat.completion.chunk",
                  "created": meta["created"], "model": name,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client stopped reading

    def _generate(self, model, meta, prompt_ids, temperature, top_k, top_p, max_new,
                  stop, emit):
        """Run one completion. emit(chunk_payload) per text piece when streaming."""

        def chunk(delta, finish=None):
            return {"id": meta["id"], "object": "chat.completion.chunk",
                    "created": meta["created"], "model": meta["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "step": meta["step"], "val_loss": meta["val_loss"],
                    "prompt_bytes": meta["prompt_bytes"]}

        decoder = StreamDecoder()
        text, used, finish, seen = [], 0, "length", ""
        with self.store.gen_lock:
            for token in decode(model, prompt_ids, temperature=temperature,
                                top_k=top_k, top_p=top_p, max_new_tokens=max_new):
                used += 1
                if token == EOS:
                    finish = "stop"
                    break
                part = decoder.feed([token])
                if part:
                    text.append(part)
                    seen += part
                    if emit is not None:
                        emit(chunk({"content": part}))
                    if stop and any(s in seen for s in stop):
                        finish = "stop"
                        break
        tail = decoder.flush()
        if tail:
            text.append(tail)
            if emit is not None:
                emit(chunk({"content": tail}))
        return "".join(text), used, finish


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def pick_device(requested):
    if requested != "auto":
        return requested
    return "cuda" if torch.cuda.is_available() else "cpu"


def main():
    ap = argparse.ArgumentParser(
        description="OpenAI-compatible server for Tensegrity checkpoints in ~/train-out.")
    ap.add_argument("--runs-dir", default=os.path.expanduser("~/train-out"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"],
                    help="cpu (default; shares the machine with training) or cuda")
    ap.add_argument("--checkpoint", default="ckpt_best.pt",
                    choices=["ckpt_best.pt", "ckpt_latest.pt"])
    ap.add_argument("--temperature", type=float, default=0.85)
    ap.add_argument("--top-k", type=int, default=40)
    ap.add_argument("--max-tokens", type=int, default=120,
                    help="default reply budget, always capped at block_size/2")
    ap.add_argument("--format", default="raw", choices=["raw", "labels"],
                    help="raw = plain prose continuation (default); "
                         "labels = system:/user:/assistant: lines")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    device = pick_device(args.device)
    runs_dir = Path(args.runs_dir).expanduser()
    store = ModelStore(runs_dir, device, args.checkpoint,
                       {"temperature": args.temperature, "top_k": args.top_k,
                        "max_tokens": args.max_tokens, "format": args.format,
                        "verbose": args.verbose})
    Handler.store = store

    runs = store.runs()
    print(f"[serve] {len(runs)} run(s) in {runs_dir} on {device}", flush=True)
    for run in runs:
        print(f"[serve]   {run['id']}  step={run['step']}  val={run['val_loss']}",
              flush=True)
    if not runs:
        print(f"[serve] no {args.checkpoint} yet - chat will 404 until you train",
              flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[serve] listening on http://{args.host}:{args.port}/v1 "
          f"(cube-chat: /run <name>)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] bye", flush=True)


if __name__ == "__main__":
    main()
