// CP Assistant: client for the local helper process (assistant-helper/), per HELPER-PROTOCOL.md.
// fetch and EventSource to 127.0.0.1 both work from inside LUNA's web view.

class HelperClient {
  constructor(base = 'http://127.0.0.1:8791') {
    this.base = base;
    this.listeners = new Set();
    this.connected = false;
    this.source = null;
  }

  /** fn(event) for every event from /events, plus { type: 'connection', connected }. */
  on(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(e) {
    for (const fn of this.listeners) fn(e);
  }

  /** Open the event stream and keep it open; EventSource reconnects by itself. */
  start() {
    if (this.source) return;
    const open = () => {
      this.source = new EventSource(`${this.base}/events`);
      this.source.onopen = () => {
        this.connected = true;
        this.emit({ type: 'connection', connected: true });
      };
      this.source.onmessage = (m) => {
        try {
          this.emit(JSON.parse(m.data));
        } catch {
          /* ignore a malformed line */
        }
      };
      this.source.onerror = () => {
        if (this.connected) this.emit({ type: 'connection', connected: false });
        this.connected = false;
        if (this.source.readyState === EventSource.CLOSED) {
          this.source = null;
          setTimeout(open, 2000);
        }
      };
    };
    open();
  }

  async request(method, path, body) {
    const r = await fetch(`${this.base}${path}`, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`helper ${path}: ${r.status}`);
    return r.json();
  }

  health() { return this.request('GET', '/health'); }
  catalog(actions) { return this.request('POST', '/catalog', { actions }); }
  context(snapshot) { return this.request('POST', '/context', snapshot); }
  listen(inputs, enabled = true) { return this.request('POST', '/listen', { inputs, enabled }); }
  prompt(text) { return this.request('POST', '/prompt', { text, source: 'text' }); }
  feedback(f) { return this.request('POST', '/feedback', f); }
  profile() { return this.request('GET', '/profile'); }
  saveRecipe(r) { return this.request('POST', '/profile/recipe', r); }
  deleteRecipe(name) { return this.request('DELETE', `/profile/recipe/${encodeURIComponent(name)}`); }
}

globalThis.HelperClient = HelperClient;
