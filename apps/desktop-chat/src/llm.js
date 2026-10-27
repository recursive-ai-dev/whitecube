const { TOOL_DEFS, DESTRUCTIVE_TOOLS, execute, previewTool } = require('./tools');

const MAX_TURNS = 25;
const MAX_RETRIES = 3;
const IDLE_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_TOKENS = 8192;
const MAX_HISTORY_CHARS = 100_000;
const FORCE_TRIM_CHARS = 30_000;
const MAX_STREAM_CHARS = 4_000_000;
const MAX_EVENT_CHARS = 1_000_000;
const MAX_TOOL_CALLS = 64;

function chatCompletionsUrl(endpoint) {
  const base = (endpoint || '').trim().replace(/\/+$/, '');
  return base.endsWith('/chat/completions') ? base : `${base}/chat/completions`;
}

// Read error responses without trusting an endpoint to send a small body.
async function errorText(response) {
  if (!response.body) return '';
  const reader = response.body.getReader();
  try {
    const { value } = await reader.read();
    return value ? new TextDecoder().decode(value.subarray(0, 2048)) : '';
  } catch { return ''; }
  finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

async function streamChatCompletion({ endpoint, apiKey, model, messages, signal, toolsEnabled = true, maxTokens = DEFAULT_MAX_TOKENS, temperature = 0.7 }, onDelta) {
  const idleController = new AbortController();
  const arm = () => setTimeout(() => idleController.abort(), IDLE_TIMEOUT_MS);
  let idleTimer = arm();
  const combinedSignal = signal ? AbortSignal.any([signal, idleController.signal]) : idleController.signal;
  let reader, content = '', buffer = '', finishReason = null, doneMarker = false, received = 0;
  const toolCalls = [];

  const consume = (line) => {
    line = line.trim();
    if (!line.startsWith('data:')) return;
    const data = line.slice(5).trim();
    if (!data) return;
    if (data === '[DONE]') { doneMarker = true; return; }
    let parsed;
    try { parsed = JSON.parse(data); }
    catch { throw new Error('Endpoint sent malformed streaming JSON.'); }
    if (parsed.error) throw new Error(parsed.error.message || 'Endpoint reported a streaming error.');
    if (parsed.usage) onDelta({ type: 'usage', usage: parsed.usage });
    const choice = parsed.choices?.[0];
    if (!choice) return;
    const delta = choice.delta || {};
    if (typeof delta.content === 'string' && delta.content) {
      content += delta.content;
      onDelta({ type: 'text-delta', text: delta.content });
    }
    if (Array.isArray(delta.tool_calls)) {
      for (const call of delta.tool_calls) {
        const index = call.index ?? 0;
        if (!Number.isInteger(index) || index < 0 || index >= MAX_TOOL_CALLS) throw new Error('Endpoint sent an invalid tool call index.');
        toolCalls[index] ||= { id: '', type: 'function', function: { name: '', arguments: '' } };
        if (typeof call.id === 'string') toolCalls[index].id = call.id;
        if (typeof call.function?.name === 'string') toolCalls[index].function.name += call.function.name;
        if (typeof call.function?.arguments === 'string') toolCalls[index].function.arguments += call.function.arguments;
      }
    }
    if (choice.finish_reason) finishReason = choice.finish_reason;
  };

  try {
    const response = await fetch(chatCompletionsUrl(endpoint), {
      method: 'POST', signal: combinedSignal,
      headers: { 'Content-Type': 'application/json', ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({ model, messages, ...(toolsEnabled ? { tools: TOOL_DEFS } : {}), stream: true, max_tokens: maxTokens, temperature })
    });
    if (!response.ok) {
      const detail = await errorText(response);
      const error = new Error(`API error ${response.status}: ${detail || response.statusText}`);
      error.status = response.status;
      const retry = response.headers.get('retry-after');
      error.retryAfter = retry !== null && Number.isFinite(Number(retry)) ? Number(retry) : null;
      throw error;
    }
    if (!response.body) throw new Error('API response had no body.');
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (!doneMarker) {
      const { done, value } = await reader.read();
      clearTimeout(idleTimer); idleTimer = arm();
      buffer += decoder.decode(value, { stream: !done });
      received += value?.byteLength || 0;
      if (received > MAX_STREAM_CHARS) throw new Error('Endpoint response exceeded the 4 MB safety limit.');
      let lineEnd;
      while ((lineEnd = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, lineEnd);
        buffer = buffer.slice(lineEnd + 1);
        if (line.length > MAX_EVENT_CHARS) throw new Error('Endpoint sent an oversized streaming event.');
        consume(line);
        if (doneMarker) break;
      }
      if (buffer.length > MAX_EVENT_CHARS) throw new Error('Endpoint sent an oversized streaming event.');
      if (done) { if (buffer.trim()) consume(buffer); break; }
    }
    if (!finishReason && !doneMarker) throw new Error('Connection ended before the reply finished. Please retry.');
    const calls = toolCalls.filter(Boolean);
    if (calls.some(call => !call.id || !call.function.name)) throw new Error('Endpoint sent an incomplete tool call.');
    return { content, toolCalls: calls, finishReason };
  } catch (original) {
    let error = original;
    if (signal?.aborted) error = new DOMException('Cancelled', 'AbortError');
    else if (idleController.signal.aborted) {
      error = new Error(content ? 'Response stalled mid-stream.' : 'Request stalled — no response from the endpoint.');
      error.isTimeout = true;
    }
    // Retrying after text reached the UI would duplicate a partial answer.
    error.partialContent = content;
    throw error;
  } finally {
    clearTimeout(idleTimer);
    if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
}

async function callWithRetry(args, onEvent) {
  for (let attempt = 0; ; attempt++) {
    try { return await streamChatCompletion(args, onEvent); }
    catch (error) {
      if (args.signal?.aborted || error.partialContent) throw error;
      const retryable = error.isTimeout || error.status === 429 || error.status >= 500 && error.status < 600 || error instanceof TypeError;
      if (!retryable || attempt >= MAX_RETRIES) throw error;
      const delay = Math.min(Math.max(error.retryAfter ?? 2 ** attempt, 0), 30);
      onEvent({ type: 'notice', text: `${error.message} — retrying in ${delay}s… (${attempt + 1}/${MAX_RETRIES})` });
      await new Promise((resolve, reject) => {
        const cancel = () => { clearTimeout(timer); args.signal?.removeEventListener('abort', cancel); reject(new DOMException('Cancelled', 'AbortError')); };
        const timer = setTimeout(() => { args.signal?.removeEventListener('abort', cancel); resolve(); }, delay * 1000);
        if (args.signal?.aborted) cancel();
        else args.signal?.addEventListener('abort', cancel, { once: true });
      });
    }
  }
}

function isContextOverflowError(error) {
  return error.status === 400 && /context.?length|context.window|maximum context|too many tokens|reduce the length/i.test(error.message);
}

// Only remove complete user turns; keep the latest user request and its tool results.
function trimHistory(messages, budgetChars) {
  while (JSON.stringify(messages).length > budgetChars) {
    const next = messages.findIndex((message, index) => index > 1 && message.role === 'user');
    if (next < 0) break;
    messages.splice(1, next - 1);
  }
}

function repairToolCalls(messages) {
  for (let index = 0; index < messages.length; index++) {
    const calls = messages[index].role === 'assistant' && messages[index].tool_calls;
    if (!Array.isArray(calls) || !calls.length) continue;
    let end = index + 1;
    while (end < messages.length && messages[end].role === 'tool') end++;
    const answered = new Set(messages.slice(index + 1, end).map(message => message.tool_call_id));
    const missing = calls.filter(call => !answered.has(call.id)).map(call => ({ role: 'tool', tool_call_id: call.id, content: 'Tool was interrupted before completion.' }));
    messages.splice(end, 0, ...missing);
    index = end + missing.length - 1;
  }
  return messages;
}

async function runAgentTurn({ settings, messages, onEvent, confirmTool, signal }) {
  repairToolCalls(messages);
  try {
    for (let turn = 0; turn < MAX_TURNS; turn++) {
      signal?.throwIfAborted();
      trimHistory(messages, MAX_HISTORY_CHARS);
      const args = { endpoint: settings.endpoint, apiKey: settings.apiKey, model: settings.model, messages, signal,
        toolsEnabled: settings.toolsEnabled, maxTokens: settings.maxTokens, temperature: settings.temperature };
      let result;
      try { result = await callWithRetry(args, onEvent); }
      catch (error) {
        if (error.partialContent) messages.push({ role: 'assistant', content: error.partialContent });
        if (isContextOverflowError(error) && messages.length > 4) {
          onEvent({ type: 'notice', text: 'Context too large for the model — dropping oldest turns and retrying…' });
          trimHistory(messages, FORCE_TRIM_CHARS);
          result = await callWithRetry(args, onEvent);
        } else throw error;
      }
      const { content, toolCalls, finishReason } = result;
      if (settings.toolsEnabled === false && toolCalls.length) throw new Error('File tools are disabled for this model.');
      if (finishReason === 'length') onEvent({ type: 'notice', text: 'Response reached the configured token limit. Ask to continue or raise the limit in Settings.' });
      if (finishReason === 'content_filter') onEvent({ type: 'notice', text: 'The endpoint filtered this reply.' });
      // A length-limited tool request cannot be executed safely, and must not leave
      // an unanswered tool-call cluster in the conversation.
      const usableCalls = finishReason === 'length' ? [] : toolCalls;
      const assistant = { role: 'assistant', content: content || null };
      if (usableCalls.length) assistant.tool_calls = usableCalls;
      messages.push(assistant);
      if (!usableCalls.length) return { messages, finalText: content };

      for (const call of usableCalls) {
        signal?.throwIfAborted();
        const name = call.function.name;
        let toolArgs;
        try {
          toolArgs = JSON.parse(call.function.arguments || '{}');
          if (!toolArgs || typeof toolArgs !== 'object' || Array.isArray(toolArgs)) throw new Error('expected an object');
        } catch (error) {
          messages.push({ role: 'tool', tool_call_id: call.id, content: `Error: invalid arguments JSON: ${error.message}` });
          onEvent({ type: 'tool-error', name, error: `Invalid arguments JSON: ${error.message}` });
          continue;
        }
        let preview = null;
        if (DESTRUCTIVE_TOOLS.has(name)) preview = await previewTool(name, toolArgs).catch(error => `(preview failed: ${error.message})`);
        onEvent({ type: 'tool-call', name, args: toolArgs, preview });
        if (DESTRUCTIVE_TOOLS.has(name) && settings.confirmDestructive !== false) {
          const approved = await confirmTool({ name, args: toolArgs, preview });
          signal?.throwIfAborted();
          if (!approved) {
            messages.push({ role: 'tool', tool_call_id: call.id, content: 'User declined to run this tool.' });
            onEvent({ type: 'tool-declined', name, args: toolArgs });
            continue;
          }
        }
        try {
          signal?.throwIfAborted();
          const output = await execute(name, toolArgs);
          messages.push({ role: 'tool', tool_call_id: call.id, content: `<untrusted_tool_output>\n${output}\n</untrusted_tool_output>` });
          onEvent({ type: 'tool-result', name, args: toolArgs, result: String(output) });
        } catch (error) {
          if (signal?.aborted) throw error;
          messages.push({ role: 'tool', tool_call_id: call.id, content: `Error: ${error.message}` });
          onEvent({ type: 'tool-error', name, args: toolArgs, error: error.message });
        }
      }
    }
    onEvent({ type: 'notice', text: 'Stopped after 25 tool rounds. Send a follow-up to continue.' });
    return { messages, finalText: '[stopped: too many tool-call turns]' };
  } finally {
    // Cancellation while waiting for approval must not corrupt the next request.
    repairToolCalls(messages);
  }
}

module.exports = { runAgentTurn, streamChatCompletion, trimHistory, repairToolCalls };
