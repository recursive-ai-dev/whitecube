const { TOOL_DEFS, DESTRUCTIVE_TOOLS, execute, previewTool } = require('./tools');

const MAX_TURNS = 25;
const MAX_RETRIES = 3;
const IDLE_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_TOKENS = 8192;
const MAX_HISTORY_CHARS = 100_000;
const FORCE_TRIM_CHARS = 30_000;

function chatCompletionsUrl(endpoint) {
  let base = (endpoint || '').trim().replace(/\/+$/, '');
  if (base.endsWith('/chat/completions')) return base;
  return `${base}/chat/completions`;
}

async function streamChatCompletion({ endpoint, apiKey, model, messages, signal, toolsEnabled = true, maxTokens = DEFAULT_MAX_TOKENS }, onDelta) {
  const url = chatCompletionsUrl(endpoint);

  const idleController = new AbortController();
  const arm = () => setTimeout(() => idleController.abort(), IDLE_TIMEOUT_MS);
  let idleTimer = arm();
  const resetIdle = () => {
    clearTimeout(idleTimer);
    idleTimer = arm();
  };
  const combinedSignal = signal ? AbortSignal.any([signal, idleController.signal]) : idleController.signal;

  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      signal: combinedSignal,
      headers: {
        'Content-Type': 'application/json',
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {})
      },
      body: JSON.stringify({
        model,
        messages,
        ...(toolsEnabled ? { tools: TOOL_DEFS } : {}),
        stream: true,
        max_tokens: maxTokens
      })
    });
  } catch (err) {
    clearTimeout(idleTimer);
    if (signal?.aborted) throw err;
    if (idleController.signal.aborted) {
      const e = new Error('Request stalled — no response from the endpoint.');
      e.isTimeout = true;
      throw e;
    }
    throw err;
  }

  if (!res.ok) {
    clearTimeout(idleTimer);
    const text = await res.text().catch(() => '');
    const err = new Error(`API error ${res.status}: ${text || res.statusText}`);
    err.status = res.status;
    const retryAfter = Number(res.headers.get('retry-after'));
    err.retryAfter = Number.isFinite(retryAfter) ? retryAfter : null;
    throw err;
  }

  if (!res.body) {
    clearTimeout(idleTimer);
    throw new Error('API response had no body');
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  let content = '';
  const toolCalls = [];
  let finishReason = null;

  try {
    while (true) {
      const { done, value } = await reader.read();
      resetIdle();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let lineEnd;
      while ((lineEnd = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, lineEnd).trim();
        buffer = buffer.slice(lineEnd + 1);
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') continue;

        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch (e) {
          continue;
        }

        const choice = parsed.choices && parsed.choices[0];
        if (!choice) continue;
        const delta = choice.delta || {};

        if (delta.content) {
          content += delta.content;
          onDelta({ type: 'text-delta', text: delta.content });
        }

        if (delta.tool_calls) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index ?? 0;
            if (!toolCalls[idx]) {
              toolCalls[idx] = { id: tc.id || '', type: 'function', function: { name: '', arguments: '' } };
            }
            if (tc.id) toolCalls[idx].id = tc.id;
            if (tc.function?.name) toolCalls[idx].function.name += tc.function.name;
            if (tc.function?.arguments) toolCalls[idx].function.arguments += tc.function.arguments;
          }
        }

        if (choice.finish_reason) finishReason = choice.finish_reason;
      }
    }
  } catch (err) {
    if (signal?.aborted) throw err;
    if (idleController.signal.aborted) {
      const e = new Error('Response stalled mid-stream.');
      e.isTimeout = true;
      throw e;
    }
    throw err;
  } finally {
    clearTimeout(idleTimer);
  }

  return { content, toolCalls: toolCalls.filter(Boolean), finishReason };
}

async function callWithRetry(args, onEvent) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await streamChatCompletion(args, onEvent);
    } catch (err) {
      if (args.signal?.aborted) throw err; // never retry a user-initiated cancel
      const retryable = err.isTimeout || err.status === 429 || (err.status >= 500 && err.status < 600) || !err.status;
      if (!retryable || attempt >= MAX_RETRIES) throw err;
      const delaySec = err.retryAfter ?? Math.min(2 ** attempt, 20);
      onEvent({ type: 'notice', text: `${err.message} — retrying in ${delaySec}s… (${attempt + 1}/${MAX_RETRIES})` });
      await new Promise((resolve, reject) => {
        const cancel = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
        const timer = setTimeout(() => { args.signal?.removeEventListener('abort', cancel); resolve(); }, Math.min(Math.max(delaySec, 0), 30) * 1000);
        if (args.signal?.aborted) cancel();
        else args.signal?.addEventListener('abort', cancel, { once: true });
      });
    }
  }
}

function isContextOverflowError(err) {
  if (err.status !== 400) return false;
  return /context.?length|context.window|maximum context|too many tokens|reduce the length/i.test(err.message);
}

// Trims oldest complete turns (never splits a tool_calls/tool_response cluster) until the
// serialized history fits the budget, or only the system prompt + latest turn remain.
function trimHistory(messages, budgetChars) {
  let total = JSON.stringify(messages).length;
  while (total > budgetChars && messages.length > 4) {
    let cut = 2;
    while (cut < messages.length && messages[cut].role !== 'user') cut++;
    messages.splice(1, cut - 1);
    total = JSON.stringify(messages).length;
  }
}

async function runAgentTurn({ settings, messages, onEvent, confirmTool, signal }) {
  for (let turn = 0; turn < MAX_TURNS; turn++) {
    trimHistory(messages, MAX_HISTORY_CHARS);

    const callArgs = { endpoint: settings.endpoint, apiKey: settings.apiKey, model: settings.model, messages, signal,
      toolsEnabled: settings.toolsEnabled, maxTokens: settings.maxTokens };
    let result;
    try {
      result = await callWithRetry(callArgs, onEvent);
    } catch (err) {
      if (isContextOverflowError(err) && messages.length > 4) {
        onEvent({ type: 'notice', text: 'Context too large for the model — dropping oldest turns and retrying…' });
        trimHistory(messages, FORCE_TRIM_CHARS);
        result = await callWithRetry(callArgs, onEvent);
      } else {
        throw err;
      }
    }

    const { content, toolCalls, finishReason } = result;
    if (settings.toolsEnabled === false && toolCalls.length) {
      throw new Error('This local checkpoint does not support tool calls.');
    }

    if (finishReason === 'length') {
      onEvent({ type: 'notice', text: '⚠ response cut off — hit the max_tokens limit' });
    }

    const assistantMsg = { role: 'assistant', content: content || null };
    if (toolCalls.length) assistantMsg.tool_calls = toolCalls;
    messages.push(assistantMsg);

    if (!toolCalls.length || finishReason !== 'tool_calls') {
      return { messages, finalText: content };
    }

    for (const call of toolCalls) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      const name = call.function.name;
      let args = {};
      try {
        args = call.function.arguments ? JSON.parse(call.function.arguments) : {};
      } catch (e) {
        const reason = finishReason === 'length' ? 'arguments were truncated by the max_tokens limit' : `invalid arguments JSON: ${e.message}`;
        messages.push({ role: 'tool', tool_call_id: call.id, content: `Error: ${reason}` });
        onEvent({ type: 'tool-error', name, error: reason });
        continue;
      }

      let preview = null;
      if (DESTRUCTIVE_TOOLS.has(name)) {
        preview = await previewTool(name, args).catch((e) => `(preview failed: ${e.message})\n\n${JSON.stringify(args, null, 2)}`);
      }
      onEvent({ type: 'tool-call', name, args, preview });

      if (DESTRUCTIVE_TOOLS.has(name) && settings.confirmDestructive) {
        const approved = await confirmTool({ name, args, preview });
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        if (!approved) {
          messages.push({ role: 'tool', tool_call_id: call.id, content: 'User declined to run this tool.' });
          onEvent({ type: 'tool-declined', name, args });
          continue;
        }
      }

      try {
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        const result = await execute(name, args);
        messages.push({
          role: 'tool',
          tool_call_id: call.id,
          content: `<untrusted_tool_output>\n${result}\n</untrusted_tool_output>`
        });
        onEvent({ type: 'tool-result', name, args, result: String(result) });
      } catch (err) {
        const msg = `Error: ${err.message}`;
        messages.push({ role: 'tool', tool_call_id: call.id, content: msg });
        onEvent({ type: 'tool-error', name, args, error: err.message });
      }
    }
  }

  return { messages, finalText: '[stopped: too many tool-call turns]' };
}

module.exports = { runAgentTurn };
