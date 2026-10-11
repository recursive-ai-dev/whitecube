/* Bounded browser API bootstrap. Only __cube crosses into the native host. */
(() => {
  'use strict';
  const call = __cube, nodes = new Map(), ids = new WeakMap(), listeners = new WeakMap();
  let listenerCount = 0, ready = 'loading';
  class Event {
    constructor(type, options = {}) {
      this.type = String(type); this.bubbles = !!options.bubbles; this.cancelable = !!options.cancelable;
      this.defaultPrevented = false; this.target = null; this.currentTarget = null;
      this.eventPhase = 0; this.isTrusted = false; this.timeStamp = Date.now();
      this._stop = this._immediate = this._passive = false;
    }
    preventDefault() { if (this.cancelable && !this._passive) this.defaultPrevented = true; }
    stopPropagation() { this._stop = true; }
    stopImmediatePropagation() { this._stop = this._immediate = true; }
  }
  class CustomEvent extends Event { constructor(type, options = {}) { super(type, options); this.detail = options.detail ?? null; } }
  class EventTarget {
    addEventListener(type, callback, options = {}) {
      if (!callback) return;
      const capture = typeof options === 'boolean' ? options : !!options.capture;
      const list = listeners.get(this) || []; type = String(type);
      if (list.some(x => x.type === type && x.callback === callback && x.capture === capture)) return;
      if (++listenerCount > 4096) throw new RangeError('Event listener limit');
      list.push({type, callback, capture, once: !!options.once, passive: !!options.passive}); listeners.set(this, list);
    }
    removeEventListener(type, callback, options = {}) {
      const capture = typeof options === 'boolean' ? options : !!options.capture;
      const list = listeners.get(this) || [], keep = list.filter(x => !(x.type === String(type) && x.callback === callback && x.capture === capture));
      listenerCount -= list.length - keep.length; listeners.set(this, keep);
    }
    dispatchEvent(event) {
      if (!(event instanceof Event) || event.currentTarget) throw new TypeError('Invalid event');
      const path = [this]; if (this instanceof Node) { let parent = this.parentNode; while (parent) { path.push(parent); parent = parent.parentNode; } if (path.at(-1) === document) path.push(globalThis); }
      event.target = this;
      const invoke = (target, capture, phase) => {
        event.currentTarget = target; event.eventPhase = phase;
        for (const entry of [...(listeners.get(target) || [])]) {
          if (entry.type !== event.type || entry.capture !== capture) continue;
          if (!(listeners.get(target) || []).includes(entry)) continue;
          if (entry.once) EventTarget.prototype.removeEventListener.call(target, entry.type, entry.callback, entry.capture);
          event._passive = entry.passive;
          try { if (typeof entry.callback === 'function') entry.callback.call(target, event); else entry.callback.handleEvent(event); } catch (e) { console.error(e); }
          event._passive = false; if (event._immediate) break;
        }
        if (!capture && !event._immediate && typeof target['on' + event.type] === 'function') {
          try { if (target['on' + event.type](event) === false) event.preventDefault(); } catch (e) { console.error(e); }
        }
      };
      for (let i = path.length - 1; i > 0 && !event._stop; i--) invoke(path[i], true, 1);
      if (!event._stop) { invoke(this, true, 2); if (!event._immediate) invoke(this, false, 2); }
      if (event.bubbles) for (let i = 1; i < path.length && !event._stop; i++) invoke(path[i], false, 3);
      event.currentTarget = null; event.eventPhase = 0; return !event.defaultPrevented;
    }
  }
  function id(node) { const n = ids.get(node); if (!n) throw new TypeError('Expected a document node'); return n; }
  function list(values) { values.item = index => values[index] ?? null; return values; }
  function wrap(n) {
    if (!n) return null; if (nodes.has(n)) return nodes.get(n);
    const type = call(2, n, 'nodeType'), value = Object.create(type === 9 ? Document.prototype : type === 3 ? Text.prototype : Element.prototype);
    ids.set(value, n); nodes.set(n, value); return value;
  }
  function query(root, selector) {
    selector = String(selector); if (!selector.trim() || selector.length > 1024) throw new SyntaxError('Invalid selector');
    return list([...new Set(selector.split(',').flatMap(s => call(1, id(root), s.trim(), 'selector')))].map(wrap));
  }
  class Node extends EventTarget {
    constructor() { super(); throw new TypeError('Use document.createElement or createTextNode'); }
    get nodeType() { return call(2, id(this), 'nodeType'); }
    get nodeName() { return call(2, id(this), 'nodeName'); }
    get parentNode() { return wrap(call(2, id(this), 'parentNode')); }
    get parentElement() { const p = this.parentNode; return p?.nodeType === 1 ? p : null; }
    get childNodes() { return list(call(2, id(this), 'childNodes').map(wrap)); }
    get firstChild() { return this.childNodes[0] ?? null; }
    get lastChild() { return this.childNodes.at(-1) ?? null; }
    get nextSibling() { return wrap(call(2, id(this), 'nextSibling')); }
    get previousSibling() { const siblings = this.parentNode?.childNodes || []; return siblings[siblings.indexOf(this) - 1] ?? null; }
    get textContent() { return call(2, id(this), 'textContent'); }
    set textContent(value) { call(3, id(this), 'textContent', value ?? ''); }
    get isConnected() { return call(2, id(this), 'isConnected'); }
    get ownerDocument() { return this === document ? null : document; }
    appendChild(child) { call(6, id(this), id(child), 0); return child; }
    insertBefore(child, before) { call(6, id(this), id(child), before == null ? 0 : id(before)); return child; }
    removeChild(child) { call(6, id(this), id(child), 'remove'); return child; }
    replaceChild(child, old) { this.insertBefore(child, old); this.removeChild(old); return old; }
    remove() { this.parentNode?.removeChild(this); }
    append(...children) { for (const child of children) this.appendChild(child instanceof Node ? child : document.createTextNode(String(child))); }
    prepend(...children) { for (const child of children.reverse()) this.insertBefore(child instanceof Node ? child : document.createTextNode(String(child)), this.firstChild); }
    replaceChildren(...children) { while (this.firstChild) this.removeChild(this.firstChild); this.append(...children); }
    contains(other) { for (let n = other; n; n = n.parentNode) if (n === this) return true; return false; }
    hasChildNodes() { return this.firstChild !== null; }
  }
  Object.assign(Node, {ELEMENT_NODE:1, TEXT_NODE:3, DOCUMENT_NODE:9});
  class Text extends Node { get data() { return this.textContent; } set data(v) { this.textContent = v; } get nodeValue() { return this.data; } set nodeValue(v) { this.data = v; } get length() { return this.data.length; } }
  class Element extends Node {
    get tagName() { return this.nodeName; }
    get localName() { return this.tagName.toLowerCase(); }
    get children() { return list(this.childNodes.filter(n => n.nodeType === 1)); }
    get firstElementChild() { return this.children[0] ?? null; }
    get lastElementChild() { return this.children.at(-1) ?? null; }
    get childElementCount() { return this.children.length; }
    getAttribute(name) { return call(4, id(this), 'get', name); }
    hasAttribute(name) { return call(4, id(this), 'has', name); }
    setAttribute(name, value) { call(4, id(this), 'set', name, value); }
    removeAttribute(name) { call(4, id(this), 'remove', name); }
    toggleAttribute(name, force) { const set = force === undefined ? !this.hasAttribute(name) : !!force; if (set) this.setAttribute(name, ''); else this.removeAttribute(name); return set; }
    querySelector(selector) { return query(this, selector)[0] ?? null; }
    querySelectorAll(selector) { return query(this, selector); }
    matches(selector) { const parent = this.parentNode; return parent ? query(parent, selector).includes(this) : false; }
    closest(selector) { for (let n = this; n; n = n.parentElement) if (n.matches(selector)) return n; return null; }
    get innerText() { return this.textContent; } set innerText(v) { this.textContent = v; }
    get innerHTML() { throw new TypeError('innerHTML is not supported; use textContent or child methods'); }
    set innerHTML(v) { throw new TypeError('innerHTML is not supported; use textContent or child methods'); }
    get classList() {
      const element = this, values = () => element.className.split(/\s+/).filter(Boolean), save = a => { element.className = [...new Set(a)].join(' '); };
      return {contains: s => values().includes(String(s)), add: (...s) => save([...values(), ...s]), remove: (...s) => save(values().filter(x => !s.includes(x))),
        toggle(s, force) { const yes = force === undefined ? !this.contains(s) : !!force; if (yes) this.add(s); else this.remove(s); return yes; },
        get value() { return element.className; }, [Symbol.iterator]: function*(){yield* values();}};
    }
    get dataset() { const element = this, attr = k => 'data-' + String(k).replace(/[A-Z]/g, c => '-' + c.toLowerCase()); return new Proxy({}, {get:(_,k) => element.getAttribute(attr(k)) ?? undefined, set:(_,k,v) => {element.setAttribute(attr(k),v);return true;}, deleteProperty:(_,k) => {element.removeAttribute(attr(k));return true;}}); }
    get style() {
      const element = this, key = k => String(k).replace(/[A-Z]/g, c => '-' + c.toLowerCase());
      const read = () => { const o = Object.create(null); for(const part of (element.getAttribute('style') || '').split(';')){const at=part.indexOf(':');if(at>0)o[part.slice(0,at).trim()]=part.slice(at+1).trim();}return o; };
      const set = (k,v) => {const o=read();if(v==='')delete o[k];else o[k]=String(v);element.setAttribute('style',Object.entries(o).map(([a,b])=>a+':'+b).join(';'));};
      return new Proxy({setProperty:(k,v,priority='')=>set(k,String(v)+(priority==='important'?' !important':'')),getPropertyValue:k=>read()[k]||'',removeProperty:k=>{const v=read()[k]||'';set(k,'');return v;}},
        {get:(o,k)=>k==='cssText'?element.getAttribute('style')||'':k in o?o[k]:read()[key(k)]||'',set:(_,k,v)=>{if(k==='cssText')element.setAttribute('style',v);else set(key(k),v);return true;}});
    }
    get value() { return call(2,id(this),'value'); } set value(v) { call(3,id(this),'value',v); }
    get checked() { return call(2,id(this),'checked'); } set checked(v) { call(3,id(this),'checked',!!v); }
    get hidden() { return call(2,id(this),'hidden'); } set hidden(v) { call(3,id(this),'hidden',!!v); }
    get disabled() { return call(2,id(this),'disabled'); } set disabled(v) { call(3,id(this),'disabled',!!v); }
    get href() { return call(2,id(this),'href'); } set href(v) { this.setAttribute('href',v); }
    get elements() { return this.querySelectorAll('input,button,textarea,select'); }
    click() { this.dispatchEvent(new Event('click',{bubbles:true,cancelable:true})); }
  }
  for(const [property,attribute] of Object.entries({id:'id',className:'class',name:'name',type:'type',action:'action',method:'method',placeholder:'placeholder'}))Object.defineProperty(Element.prototype,property,{get(){return this.getAttribute(attribute)||'';},set(v){this.setAttribute(attribute,v);}});
  class Document extends Node {
    get documentElement() { return this.querySelector('html'); }
    get head() { return this.querySelector('head'); }
    get body() { return this.querySelector('body'); }
    get readyState() { return ready; }
    get URL() { return call(8); } get documentURI() { return this.URL; }
    get title() { return call(2,id(this),'title'); } set title(v) { call(3,id(this),'title',v); }
    get activeElement() { return wrap(call(2,id(this),'activeElement')) || this.body; }
    get defaultView() { return globalThis; }
    querySelector(selector) { return query(this,selector)[0] ?? null; }
    querySelectorAll(selector) { return query(this,selector); }
    getElementById(value) { return wrap(call(1,id(this),value,'id')[0] || 0); }
    getElementsByTagName(tag) { return this.querySelectorAll(String(tag)); }
    getElementsByClassName(name) { return this.querySelectorAll('.'+String(name).trim().split(/\s+/).join('.')); }
    createElement(tag) { return wrap(call(5,tag,'')); }
    createTextNode(text) { return wrap(call(5,'#text',text)); }
    write() { throw new TypeError('document.write is unsupported'); }
  }
  const document = wrap(1), timers = new Map(), fetches = new Map(); let nextTimer = 1, nextFetch = 1;
  const protectedGlobal = (name,value) => Object.defineProperty(globalThis,name,{value,writable:false,configurable:false});
  Object.assign(globalThis,{Event,CustomEvent,EventTarget,Node,Text,Element,HTMLElement:Element,Document,window:globalThis,self:globalThis,document});
  for(const name of ['addEventListener','removeEventListener','dispatchEvent'])globalThis[name]=EventTarget.prototype[name].bind(globalThis);
  protectedGlobal('__cubeDispatch',(n,type,data) => {
    const target=wrap(n);if(!target)return true;const event=new Event(type,{bubbles:!['focus','blur'].includes(type),cancelable:['click','keydown','beforeinput','submit'].includes(type)});
    event.isTrusted=true;event.key=({enter:'Enter',escape:'Escape',space:' ',left:'ArrowLeft',right:'ArrowRight',up:'ArrowUp',down:'ArrowDown'})[data]||data;event.data=data;return target.dispatchEvent(event);
  });
  protectedGlobal('__cubeLoaded',()=>{ready='interactive';document.dispatchEvent(new Event('DOMContentLoaded',{bubbles:true}));ready='complete';globalThis.dispatchEvent(new Event('load'));});
  const schedule=(callback,delay,interval,args)=>{if(typeof callback!=='function')throw new TypeError('Timers require a function');const n=nextTimer++;call(9,'set',n,Number(delay)||0,interval);timers.set(n,{callback,args,interval});return n;};
  globalThis.setTimeout=(callback,delay=0,...args)=>schedule(callback,delay,false,args);
  globalThis.setInterval=(callback,delay=0,...args)=>schedule(callback,delay,true,args);
  globalThis.clearTimeout=globalThis.clearInterval=n=>{timers.delete(Number(n));call(9,'clear',Number(n),0,false);};
  protectedGlobal('__cubeTimer',n=>{const timer=timers.get(n);if(!timer)return;if(!timer.interval)timers.delete(n);timer.callback(...timer.args);});
  globalThis.queueMicrotask=callback=>{if(typeof callback!=='function')throw new TypeError('Expected a function');Promise.resolve().then(callback);};
  class Response {
    constructor(body='',options={}){this._body=String(body);this.status=options.status??200;this.url=options.url||'';this.ok=this.status>=200&&this.status<300;this.bodyUsed=false;}
    text(){if(this.bodyUsed)return Promise.reject(new TypeError('Body already used'));this.bodyUsed=true;return Promise.resolve(this._body);}
    json(){return this.text().then(JSON.parse);}
  }
  globalThis.Response=Response;
  globalThis.fetch=(url,options={})=>new Promise((resolve,reject)=>{
    if((options.method||'GET').toUpperCase()!=='GET'||options.body!=null||options.headers)throw new TypeError('fetch supports GET without custom headers');
    const credentials=options.credentials||'same-origin';if(!['omit','same-origin'].includes(credentials))throw new TypeError('Unsupported credentials mode');
    const n=nextFetch++;call(10,n,String(url),credentials);fetches.set(n,{resolve,reject});
  });
  protectedGlobal('__cubeFetch',(n,status,body,url)=>{const f=fetches.get(n);if(!f)return;fetches.delete(n);if(!status)f.reject(new TypeError('Fetch blocked or failed'));else f.resolve(new Response(body,{status,url}));});
  class Storage {
    constructor(area,initial){this._area=area;this._data=Object.assign(Object.create(null),initial);}
    get length(){return Object.keys(this._data).length;}
    key(index){return Object.keys(this._data)[Number(index)]??null;}
    getItem(key){return Object.hasOwn(this._data,String(key))?this._data[String(key)]:null;}
    setItem(key,value){key=String(key);value=String(value);const data=Object.assign(Object.create(null),this._data,{[key]:value});const entries=Object.entries(data);const bytes=s=>unescape(encodeURIComponent(s)).length;const encoded=s=>bytes(s)+2+[...s].reduce((n,c)=>n+(c.charCodeAt(0)<32?5:c==='"'||c==='\\'?1:0),0);if(bytes(key)>1024||bytes(value)>16384||entries.length>128||entries.reduce((n,[k,v])=>n+encoded(k)+encoded(v)+2,2)>262144)throw new RangeError('Storage quota exceeded');call(11,this._area,'set',key,value);this._data=data;}
    removeItem(key){key=String(key);call(11,this._area,'remove',key,'');delete this._data[key];}
    clear(){call(11,this._area,'clear','','');this._data=Object.create(null);}
  }
  protectedGlobal('__cubeInit',(local,session)=>{protectedGlobal('localStorage',new Storage('local',local));protectedGlobal('sessionStorage',new Storage('session',session));});
  const href=call(8), parts=/^(https?):\/\/([^/:?#]+)(?::(\d+))?([^?#]*)(\?[^#]*)?(#.*)?$/.exec(href);
  protectedGlobal('location',Object.freeze({href,toString:()=>href,protocol:parts?parts[1]+':':'',hostname:parts?.[2]||'',port:parts?.[3]||'',pathname:parts?.[4]||'/',search:parts?.[5]||'',hash:parts?.[6]||'',assign(){throw new TypeError('Scripted navigation is unsupported');},replace(){throw new TypeError('Scripted navigation is unsupported');}}));
  Object.defineProperty(Document.prototype,'location',{get(){return globalThis.location;}});
})();
