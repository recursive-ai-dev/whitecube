/* Bounded browser API bootstrap. Only __cube crosses into the native host. */
(() => {
  'use strict';
  const call = __cube, nodes = new Map(), ids = new WeakMap(), listeners = new WeakMap();
  let listenerCount = 0, ready = 'loading';
  const dispatching=new WeakSet();
  class Event {
    constructor(type, options = {}) {
      this.type = String(type); this.bubbles = !!options.bubbles; this.cancelable = !!options.cancelable;
      this.defaultPrevented = false; this.target = null; this.currentTarget = null;
      this.eventPhase = 0; this.isTrusted = false; this.timeStamp = Date.now();
      this._stop = this._immediate = this._passive = false;
    }
    initEvent(type,bubbles=false,cancelable=false) { if(dispatching.has(this))return;this.type=String(type);this.bubbles=!!bubbles;this.cancelable=!!cancelable;this.defaultPrevented=false;this.target=null;this._stop=this._immediate=false; }
    preventDefault() { if (this.cancelable && !this._passive) this.defaultPrevented = true; }
    stopPropagation() { this._stop = true; }
    stopImmediatePropagation() { this._stop = this._immediate = true; }
  }
  class CustomEvent extends Event { constructor(type, options = {}) { super(type, options); this.detail = options.detail ?? null; } initCustomEvent(type,bubbles=false,cancelable=false,detail=null){if(dispatching.has(this))return;this.initEvent(type,bubbles,cancelable);this.detail=detail;} }
  class EventTarget {
    addEventListener(type, callback, options = {}) {
      if (callback==null) return;
      if(typeof callback!=='function'&&typeof callback!=='object')throw new TypeError('Invalid event listener');
      options = options ?? {};
      const capture = typeof options === 'boolean' ? options : !!options.capture;
      const list = listeners.get(this) || []; type = String(type);
      if (list.some(x => x.type === type && x.callback === callback && x.capture === capture)) return;
      const signal=typeof options==='object'?options.signal:null;
      if(signal!=null){const state=abortStates.get(signal);if(!state)throw new TypeError('Invalid AbortSignal');if(state.aborted)return;}
      if (listenerCount >= 4096) throw new RangeError('Event listener limit');
      const entry={type,callback,capture,once:!!options.once,passive:!!options.passive,cleanup:()=>{}};
      if(signal!=null)entry.cleanup=watchAbort(signal,()=>EventTarget.prototype.removeEventListener.call(this,type,callback,capture));
      list.push(entry);listeners.set(this,list);listenerCount++;
    }
    removeEventListener(type, callback, options = {}) {
      options = options ?? {};
      const capture = typeof options === 'boolean' ? options : !!options.capture;
      const list = listeners.get(this) || [], keep = list.filter(x => !(x.type === String(type) && x.callback === callback && x.capture === capture));
      for(const entry of list)if(!keep.includes(entry))entry.cleanup();
      listenerCount -= list.length - keep.length; listeners.set(this, keep);
      if(mediaLists.has(this))syncMedia(this);
    }
    dispatchEvent(event) {
      if (!(event instanceof Event))throw new TypeError('Invalid event');
      if(dispatching.has(event)||!event.type)throw new DOMException('Event is uninitialized or dispatching','InvalidStateError');
      const path = [this]; if (this instanceof Node) { let parent = this.parentNode; while (parent) { path.push(parent); parent = parent.parentNode; } if (path.at(-1) === document) path.push(globalThis); }
      dispatching.add(event);event.target = this;event._stop=event._immediate=event._passive=false;
      try {
      const invoke = (target, capture, phase) => {
        event.currentTarget = target; event.eventPhase = phase;
        for (const entry of [...(listeners.get(target) || [])]) {
          if (entry.type !== event.type || entry.capture !== capture) continue;
          if (!(listeners.get(target) || []).includes(entry)) continue;
          if (entry.once) EventTarget.prototype.removeEventListener.call(target, entry.type, entry.callback, entry.capture);
          event._passive = entry.passive;
          try { if (typeof entry.callback === 'function') entry.callback.call(target, event); else if(typeof entry.callback.handleEvent==='function')entry.callback.handleEvent(event); } catch (e) { console.error(e); }
          event._passive = false; if (event._immediate) break;
        }
        if (!capture && !event._immediate && typeof target['on' + event.type] === 'function') {
          try { if (target['on' + event.type](event) === false) event.preventDefault(); } catch (e) { console.error(e); }
        }
      };
      for (let i = path.length - 1; i > 0 && !event._stop; i--) invoke(path[i], true, 1);
      if (!event._stop) { invoke(this, true, 2); if (!event._immediate) invoke(this, false, 2); }
      if (event.bubbles) for (let i = 1; i < path.length && !event._stop; i++) invoke(path[i], false, 3);
      return !event.defaultPrevented;
      }finally{dispatching.delete(event);event.currentTarget=null;event.eventPhase=0;event._stop=event._immediate=event._passive=false;}
    }
  }
  class DOMException extends Error {
    constructor(message='',name='Error'){super(String(message));this.name=String(name);}
    get code(){const codes={AbortError:20,TimeoutError:23,InvalidStateError:11,NotSupportedError:9,NoModificationAllowedError:7};return Object.hasOwn(codes,this.name)?codes[this.name]:0;}
  }
  const abortStates=new WeakMap(),controllers=new WeakMap();let abortAlgorithmCount=0;
  function makeSignal(){const signal=Object.create(AbortSignal.prototype);abortStates.set(signal,{aborted:false,reason:undefined,algorithms:new Set()});return signal;}
  function watchAbort(signal,callback){
    const state=abortStates.get(signal);if(!state)throw new TypeError('Expected an AbortSignal');
    if(state.aborted){callback();return ()=>{};}
    if(abortAlgorithmCount>=4096)throw new RangeError('Abort subscription limit');
    state.algorithms.add(callback);abortAlgorithmCount++;
    return ()=>{if(state.algorithms.delete(callback))abortAlgorithmCount--;};
  }
  function abortSignal(signal,reason){
    const state=abortStates.get(signal);if(state.aborted)return;
    state.aborted=true;state.reason=reason===undefined?new DOMException('The operation was aborted.','AbortError'):reason;
    const callbacks=[...state.algorithms];abortAlgorithmCount-=callbacks.length;state.algorithms.clear();
    for(const callback of callbacks){try{callback();}catch(error){console.error(error);}}
    signal.dispatchEvent(new Event('abort'));
  }
  class AbortSignal extends EventTarget {
    constructor(){super();throw new TypeError('Illegal constructor');}
    get aborted(){return abortStates.get(this).aborted;}
    get reason(){return abortStates.get(this).reason;}
    throwIfAborted(){const state=abortStates.get(this);if(!state)throw new TypeError('Invalid AbortSignal');if(state.aborted)throw state.reason;}
    static abort(reason){const signal=makeSignal();abortSignal(signal,reason);return signal;}
    static timeout(milliseconds){
      milliseconds=Number(milliseconds);if(!Number.isInteger(milliseconds)||milliseconds<0||milliseconds>60000)throw new RangeError('Timeout must be between 0 and 60000 ms');
      const signal=makeSignal();setTimeout(()=>abortSignal(signal,new DOMException('The operation timed out.','TimeoutError')),milliseconds);return signal;
    }
  }
  class AbortController {
    constructor(){controllers.set(this,makeSignal());}
    get signal(){return controllers.get(this);}
    abort(reason){const signal=controllers.get(this);if(!signal)throw new TypeError('Invalid AbortController');abortSignal(signal,reason);}
  }
  function id(node) { const n = ids.get(node); if (!n) throw new TypeError('Expected a document node'); return n; }
  function list(values) { values.item = index => values[index] ?? null; return values; }
  function wrap(n) {
    if (!n) return null; if (nodes.has(n)) return nodes.get(n);
    const type = call(2, n, 'nodeType'), value = Object.create(type === 9 ? Document.prototype : type === 3 ? Text.prototype : call(2,n,'nodeName')==='IMG'?HTMLImageElement.prototype:HTMLElement.prototype);
    ids.set(value, n); nodes.set(n, value); return value;
  }
  function query(root, selector) {
    selector = String(selector); if (!selector.trim() || selector.length > 1024) throw new SyntaxError('Invalid selector');
    return list(call(1, id(root), selector, 'selector').map(wrap));
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
    replaceChild(child, old) { call(20,id(this),id(child),id(old)); return old; }
    cloneNode(deep=false) { return wrap(call(21,id(this),!!deep)); }
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
    get nextElementSibling() { let n = this.nextSibling; while (n && n.nodeType !== 1) n = n.nextSibling; return n; }
    get previousElementSibling() { let n = this.previousSibling; while (n && n.nodeType !== 1) n = n.previousSibling; return n; }
    get childElementCount() { return this.children.length; }
    getAttribute(name) { return call(4, id(this), 'get', name); }
    hasAttribute(name) { return call(4, id(this), 'has', name); }
    setAttribute(name, value) {
      name=String(name);value=String(value);call(4, id(this), 'set', name, value);
    }
    removeAttribute(name) { call(4, id(this), 'remove', name); }
    toggleAttribute(name, force) { const set = force === undefined ? !this.hasAttribute(name) : !!force; if (set) this.setAttribute(name, ''); else this.removeAttribute(name); return set; }
    querySelector(selector) { return query(this, selector)[0] ?? null; }
    querySelectorAll(selector) { return query(this, selector); }
    matches(selector) { selector = String(selector); if (!selector.trim() || selector.length > 1024) throw new SyntaxError('Invalid selector'); return call(13, id(this), selector); }
    closest(selector) { for (let n = this; n; n = n.parentElement) if (n.matches(selector)) return n; return null; }
    getElementsByTagName(tag) { return this.querySelectorAll(String(tag)); }
    getElementsByClassName(name) { return this.querySelectorAll('.'+String(name).trim().split(/\s+/).join('.')); }
    get innerText() { return this.textContent; } set innerText(v) { this.textContent = v; }
    get innerHTML() { return call(2, id(this), 'innerHTML'); }
    set innerHTML(v) { call(12, id(this), 'replace', v ?? ''); }
    get outerHTML() { return call(2, id(this), 'outerHTML'); }
    insertAdjacentHTML(position, html) {
      position = String(position).toLowerCase();
      if (!['beforebegin','afterbegin','beforeend','afterend'].includes(position)) throw new SyntaxError('Invalid insertion position');
      call(12, id(this), position, String(html));
    }
    insertAdjacentElement(position, element) {
      if (!(element instanceof Element)) throw new TypeError('Expected an element');
      position = String(position).toLowerCase();
      if (position === 'beforebegin') { if (!this.parentNode) return null; this.parentNode.insertBefore(element,this); }
      else if (position === 'afterend') { if (!this.parentNode) return null; this.parentNode.insertBefore(element,this.nextSibling); }
      else if (position === 'afterbegin') this.insertBefore(element,this.firstChild);
      else if (position === 'beforeend') this.appendChild(element);
      else throw new SyntaxError('Invalid insertion position');
      return element;
    }
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
    get src() { const value=this.getAttribute('src');return value===null?'':call(7,value)||value; } set src(v) { this.setAttribute('src',v); }
    getBoundingClientRect() { return rectangle(...call(15,id(this))); }
    getClientRects() { const r=call(15,id(this));return list(r[5]?[rectangle(...r)]:[]); }
    get clientWidth() { return this===document.documentElement?globalThis.innerWidth:this.getBoundingClientRect().width; }
    get clientHeight() { return this===document.documentElement?globalThis.innerHeight:this.getBoundingClientRect().height; }
    get scrollHeight() { return this===document.documentElement||this===document.body?call(14,'height'):this.clientHeight; }
    get scrollTop() { return this===document.documentElement?globalThis.scrollY:0; }
    get elements() { return this.querySelectorAll('input,button,textarea,select'); }
    click() { this.dispatchEvent(new Event('click',{bubbles:true,cancelable:true})); }
  }
  class HTMLElement extends Element {}
  class HTMLImageElement extends HTMLElement {
    get complete() { return call(2,id(this),'complete'); }
    get naturalWidth() { return call(2,id(this),'naturalWidth'); }
    get naturalHeight() { return call(2,id(this),'naturalHeight'); }
    get currentSrc(){return call(2,id(this),'currentSrc');}
    get width(){return call(2,id(this),'imageWidth');}set width(value){this.setAttribute('width',Math.max(0,Math.min(32767,Number(value)||0)));}
    get height(){return call(2,id(this),'imageHeight');}set height(value){this.setAttribute('height',Math.max(0,Math.min(32767,Number(value)||0)));}
  }
  for(const [property,attribute] of Object.entries({id:'id',className:'class',name:'name',type:'type',action:'action',method:'method',placeholder:'placeholder'}))Object.defineProperty(Element.prototype,property,{get(){return this.getAttribute(attribute)||'';},set(v){this.setAttribute(attribute,v);}});
  class Document extends Node {
    get documentElement() { return this.querySelector('html'); }
    get head() { return this.querySelector('head'); }
    get body() { return this.querySelector('body'); }
    get readyState() { return ready; }
    get currentScript() { return wrap(call(14,'currentScript')); }
    get images() { return this.querySelectorAll('img'); }
    get scripts() { return this.querySelectorAll('script'); }
    get scrollingElement() { return this.documentElement; }
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
    createEvent(type){type=String(type).toLowerCase();if(['event','events','htmlevents'].includes(type))return new Event('');if(type==='customevent')return new CustomEvent('');throw new DOMException('Unsupported event interface','NotSupportedError');}
    write() { throw new TypeError('document.write is unsupported'); }
  }
  const document = wrap(1), timers = new Map(), fetches = new Map(); let nextTimer = 1, nextFetch = 1;
  const protectedGlobal = (name,value) => Object.defineProperty(globalThis,name,{value,writable:false,configurable:false});
  Object.assign(globalThis,{Event,CustomEvent,EventTarget,DOMException,AbortSignal,AbortController,Node,Text,Element,HTMLElement,HTMLImageElement,Document,window:globalThis,self:globalThis,document});
  const computedOwners=new WeakMap();
  const computedValues=value=>{const element=computedOwners.get(value);if(!element)throw new TypeError('Invalid CSSStyleDeclaration');return call(19,id(element));};
  const readonlyStyle=()=>{throw new DOMException('Computed styles are read-only','NoModificationAllowedError');};
  class CSSStyleDeclaration {
    constructor(){throw new TypeError('Illegal constructor');}
    getPropertyValue(name){const values=computedValues(this);name=String(name);return Object.hasOwn(values,name)?values[name]:'';}
    getPropertyPriority(){return '';}
    get length(){return Object.keys(computedValues(this)).length;}
    item(index){return Object.keys(computedValues(this))[Number(index)]??'';}
    get cssText(){return '';}
    setProperty(){readonlyStyle();} removeProperty(){readonlyStyle();}
    *[Symbol.iterator](){yield* Object.keys(computedValues(this));}
  }
  globalThis.CSSStyleDeclaration=CSSStyleDeclaration;
  globalThis.getComputedStyle=(element,pseudo=null)=>{
    if(!(element instanceof Element))throw new TypeError('Expected an element');
    if(pseudo!=null&&String(pseudo)!=='')throw new DOMException('Pseudo-element styles are unsupported','NotSupportedError');
    const target=Object.create(CSSStyleDeclaration.prototype);
    const value=new Proxy(target,{get(object,key,receiver){
      if(typeof key==='symbol'||key in object)return Reflect.get(object,key,receiver);
      if(/^\d+$/.test(key))return receiver.item(Number(key));
      return receiver.getPropertyValue(key==='cssFloat'?'float':key.replace(/[A-Z]/g,c=>'-'+c.toLowerCase()));
    },set:readonlyStyle,deleteProperty:readonlyStyle,defineProperty:readonlyStyle,setPrototypeOf:readonlyStyle});
    computedOwners.set(value,element);return value;
  };
  class Navigator { constructor() { throw new TypeError('Illegal constructor'); } }
  const navigatorValues = Object.freeze({userAgent:call(14,'userAgent'),appVersion:call(14,'appVersion'),
    appCodeName:'Mozilla',appName:'Netscape',platform:'Linux',product:'Gecko',productSub:'20030107',vendor:'',vendorSub:'',
    language:'en-US',languages:Object.freeze(['en-US']),maxTouchPoints:0,hardwareConcurrency:1});
  for(const [name,value] of Object.entries(navigatorValues))Object.defineProperty(Navigator.prototype,name,{get(){return value;},enumerable:true});
  Object.defineProperty(Navigator.prototype,Symbol.toStringTag,{value:'Navigator'});
  const navigator=Object.create(Navigator.prototype);
  Object.assign(globalThis,{Navigator});protectedGlobal('navigator',navigator);protectedGlobal('clientInformation',navigator);
  function rectangle(x=0,y=0,width=0,height=0) { return {x,y,width,height,top:y,left:x,right:x+width,bottom:y+height,toJSON(){return {x,y,width,height,top:y,left:x,right:x+width,bottom:y+height};}}; }
  let viewport=call(14,'viewport');
  for(const [name,index] of Object.entries({innerWidth:0,innerHeight:1,scrollY:2,pageYOffset:2}))Object.defineProperty(globalThis,name,{get(){return viewport[index];}});
  for(const name of ['scrollX','pageXOffset'])Object.defineProperty(globalThis,name,{get(){return 0;}});
  globalThis.Image=function Image(width,height){const image=document.createElement('img');if(width!==undefined)image.setAttribute('width',Number(width));if(height!==undefined)image.setAttribute('height',Number(height));return image;};
  globalThis.Image.prototype=HTMLImageElement.prototype;
  globalThis.performance=Object.freeze({timeOrigin:Date.now(),now:()=>call(14,'time')});
  const mediaLists=new WeakMap(),activeMedia=new Set();
  function syncMedia(value){
    const state=mediaLists.get(value),active=typeof state.onchange==='function'||(listeners.get(value)||[]).some(entry=>entry.type==='change');
    if(active){if(!activeMedia.has(value)&&activeMedia.size>=128)throw new RangeError('Active media listener limit');activeMedia.add(value);}
    else activeMedia.delete(value);
  }
  class MediaQueryList extends EventTarget {
    constructor(){super();throw new TypeError('Use matchMedia');}
    get media(){return mediaLists.get(this).query;}
    get matches(){return call(16,this.media);}
    get onchange(){return mediaLists.get(this).onchange;}
    set onchange(callback){const state=mediaLists.get(this),old=state.onchange;state.onchange=typeof callback==='function'?callback:null;try{syncMedia(this);}catch(error){state.onchange=old;throw error;}}
    addEventListener(type,callback,options){
      if(String(type)==='change'&&callback!=null&&!activeMedia.has(this)&&activeMedia.size>=128)throw new RangeError('Active media listener limit');
      super.addEventListener(type,callback,options);syncMedia(this);
    }
    removeEventListener(type,callback,options){super.removeEventListener(type,callback,options);syncMedia(this);}
    addListener(callback){this.addEventListener('change',callback);}
    removeListener(callback){this.removeEventListener('change',callback);}
  }
  globalThis.MediaQueryList=MediaQueryList;
  globalThis.matchMedia=query=>{query=String(query);if(query.length>2048)throw new RangeError('Media query limit');const value=Object.create(MediaQueryList.prototype);mediaLists.set(value,{query,matches:call(16,query),onchange:null});return value;};
  const observers=new Map(),observerStates=new WeakMap();let observedTargets=0;
  class IntersectionObserver {
    constructor(callback,options={}){
      if(typeof callback!=='function')throw new TypeError('Expected a callback');options=options ?? {};
      if(options.root!=null)throw new TypeError('Only viewport intersection roots are supported');
      const marginText=String(options.rootMargin ?? '0px');if(marginText.length>256)throw new SyntaxError('Invalid rootMargin');
      const margin=marginText.trim().split(/\s+/);
      if(margin.length>4||margin.some(v=>!/^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:px|%)$/.test(v)))throw new SyntaxError('Invalid rootMargin');
      const values=margin.map(v=>({value:parseFloat(v),percent:v.endsWith('%')}));
      if(values.some(v=>!Number.isFinite(v.value)||Math.abs(v.value)>1000000))throw new SyntaxError('Invalid rootMargin');
      const margins=[values[0],values[1]||values[0],values[2]||values[0],values[3]||values[1]||values[0]];
      let thresholds=options.threshold ?? 0;
      if(Array.isArray(thresholds)&&thresholds.length>128)throw new RangeError('Invalid threshold');
      thresholds=Array.isArray(thresholds)?Array.from(thresholds,Number):[Number(thresholds)];
      if(thresholds.length>128||thresholds.some(v=>!Number.isFinite(v)||v<0||v>1))throw new RangeError('Invalid threshold');
      thresholds=[...new Set(thresholds.length?thresholds:[0])].sort((a,b)=>a-b);
      Object.defineProperties(this,{root:{value:null},rootMargin:{value:[margin[0],margin[1]||margin[0],margin[2]||margin[0],margin[3]||margin[1]||margin[0]].join(' ')},thresholds:{value:Object.freeze(thresholds)}});
      observerStates.set(this,{callback,margins,targets:new Map(),records:[],scheduled:false,epoch:0});
    }
    observe(target){
      if(!(target instanceof Element))throw new TypeError('Expected an element');
      const state=observerStates.get(this);if(state.targets.has(target))return;
      if(observedTargets>=1024||(!observers.has(this)&&observers.size>=64))throw new RangeError('Intersection observer limit');
      state.targets.set(target,null);observedTargets++;observers.set(this,state);call(17);
    }
    unobserve(target){const state=observerStates.get(this);if(state.targets.delete(target))observedTargets--;if(!state.targets.size)observers.delete(this);}
    disconnect(){const state=observerStates.get(this);observedTargets-=state.targets.size;state.targets.clear();state.records=[];state.epoch++;state.scheduled=false;observers.delete(this);}
    takeRecords(){const state=observerStates.get(this),records=state.records;state.records=[];return records;}
  }
  globalThis.IntersectionObserver=IntersectionObserver;
  protectedGlobal('__cubeViewport',()=>{
    const previous=viewport;viewport=call(14,'viewport');
    if(previous[0]!==viewport[0]||previous[1]!==viewport[1])globalThis.dispatchEvent(new Event('resize'));
    if(previous[2]!==viewport[2])document.dispatchEvent(new Event('scroll',{bubbles:true}));
    for(const value of [...activeMedia]){const state=mediaLists.get(value),matches=value.matches;if(matches!==state.matches){state.matches=matches;const event=new Event('change');event.matches=matches;event.media=value.media;value.dispatchEvent(event);}syncMedia(value);}
    for(const [observer,state] of [...observers]){
      const margins=state.margins.map(m=>m.value*(m.percent?viewport[0]/100:1));
      const root=rectangle(-margins[3],-margins[0],viewport[0]+margins[1]+margins[3],viewport[1]+margins[0]+margins[2]),entries=[];
      for(const [target,previous] of [...state.targets]){
        const box=call(15,id(target)),bounds=rectangle(...box);
        const left=Math.max(bounds.left,root.left),top=Math.max(bounds.top,root.top),right=Math.min(bounds.right,root.right),bottom=Math.min(bounds.bottom,root.bottom);
        const intersects=box[4]&&target.isConnected&&root.width>=0&&root.height>=0&&left<=right&&top<=bottom;
        const intersection=intersects?rectangle(left,top,Math.max(0,right-left),Math.max(0,bottom-top)):rectangle();
        const ratio=bounds.width*bounds.height?Math.max(0,Math.min(1,intersection.width*intersection.height/(bounds.width*bounds.height))):intersects?1:0;
        const threshold=observer.thresholds.filter(t=>t<=ratio).length;
        if(previous===null||previous.intersects!==intersects||previous.threshold!==threshold){
          state.targets.set(target,{intersects,threshold});entries.push({target,time:performance.now(),rootBounds:root,boundingClientRect:bounds,intersectionRect:intersection,isIntersecting:intersects,intersectionRatio:ratio});
        }
      }
      if(entries.length){
        state.records.push(...entries);if(state.records.length>1024)state.records.splice(0,state.records.length-1024);
        if(!state.scheduled){state.scheduled=true;const epoch=state.epoch;queueMicrotask(()=>{
          if(epoch!==state.epoch)return;state.scheduled=false;const records=observer.takeRecords();
          if(records.length){try{state.callback.call(observer,records,observer);}catch(error){console.error(error);}}
        });}
      }
    }
  });
  for(const name of ['addEventListener','removeEventListener','dispatchEvent'])globalThis[name]=EventTarget.prototype[name].bind(globalThis);
  protectedGlobal('__cubeDispatch',(n,type,data) => {
    const target=wrap(n);if(!target)return true;const event=new Event(type,{bubbles:!['focus','blur','load','error'].includes(type),cancelable:['click','keydown','beforeinput','submit'].includes(type)});
    event.isTrusted=true;event.key=({enter:'Enter',escape:'Escape',space:' ',left:'ArrowLeft',right:'ArrowRight',up:'ArrowUp',down:'ArrowDown'})[data]||data;event.data=data;return target.dispatchEvent(event);
  });
  protectedGlobal('__cubeLoaded',()=>{ready='interactive';document.dispatchEvent(new Event('DOMContentLoaded',{bubbles:true}));ready='complete';globalThis.dispatchEvent(new Event('load'));});
  const schedule=(callback,delay,interval,args)=>{if(typeof callback!=='function')throw new TypeError('Timers require a function');const n=nextTimer++;call(9,'set',n,Number(delay)||0,interval);timers.set(n,{callback,args,interval});return n;};
  globalThis.setTimeout=(callback,delay=0,...args)=>schedule(callback,delay,false,args);
  globalThis.setInterval=(callback,delay=0,...args)=>schedule(callback,delay,true,args);
  globalThis.clearTimeout=globalThis.clearInterval=n=>{timers.delete(Number(n));call(9,'clear',Number(n),0,false);};
  globalThis.requestAnimationFrame=callback=>{if(typeof callback!=='function')throw new TypeError('Expected a callback');return setTimeout(()=>callback(performance.now()),16);};
  globalThis.cancelAnimationFrame=n=>clearTimeout(n);
  protectedGlobal('__cubeTimer',n=>{const timer=timers.get(n);if(!timer)return;if(!timer.interval)timers.delete(n);timer.callback(...timer.args);});
  globalThis.queueMicrotask=callback=>{if(typeof callback!=='function')throw new TypeError('Expected a function');Promise.resolve().then(callback);};
  class Response {
    constructor(body='',options={}){this._body=String(body);this.status=options.status??200;this.url=options.url||'';this.ok=this.status>=200&&this.status<300;this.bodyUsed=false;}
    text(){if(this.bodyUsed)return Promise.reject(new TypeError('Body already used'));this.bodyUsed=true;return Promise.resolve(this._body);}
    json(){return this.text().then(JSON.parse);}
  }
  globalThis.Response=Response;
  globalThis.fetch=(url,options={})=>new Promise((resolve,reject)=>{
    options=options ?? {};const target=String(url),signal=options.signal ?? null;
    if(String(options.method||'GET').toUpperCase()!=='GET'||options.body!=null||options.headers)throw new TypeError('fetch supports GET without custom headers');
    const credentials=options.credentials||'same-origin';if(!['omit','same-origin'].includes(credentials))throw new TypeError('Unsupported credentials mode');
    const signalState=signal===null?null:abortStates.get(signal);
    if(signal!==null&&!signalState)throw new TypeError('Expected an AbortSignal');
    if(signalState?.aborted){reject(signalState.reason);return;}
    if(nextFetch>0xffffffff)throw new RangeError('Fetch identifier limit');
    const n=nextFetch++,request={resolve,reject,cleanup:()=>{}};
    if(signal)request.cleanup=watchAbort(signal,()=>{
      if(!fetches.delete(n))return;request.cleanup();
      try{call(18,n);}finally{reject(signalState.reason);}
    });
    fetches.set(n,request);
    try{call(10,n,target,credentials);}catch(error){fetches.delete(n);request.cleanup();reject(error);}
  });
  protectedGlobal('__cubeFetch',(n,status,body,url)=>{const f=fetches.get(n);if(!f)return;fetches.delete(n);f.cleanup();if(!status)f.reject(new TypeError('Fetch blocked or failed'));else f.resolve(new Response(body,{status,url}));});
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
  const location=Object.create(null);
  for(const name of ['href','protocol','origin','hostname','host','port','pathname','search','hash'])Object.defineProperty(location,name,{enumerable:true,get(){return call(22)[name];}});
  for(const name of ['assign','replace','reload'])Object.defineProperty(location,name,{value(){throw new TypeError('Scripted navigation is unsupported');}});
  Object.defineProperty(location,'toString',{value(){return call(8);}});
  Object.freeze(location);protectedGlobal('location',location);
  Object.defineProperty(globalThis,'origin',{get(){return call(14,'origin');},configurable:false});
  Object.defineProperty(Document.prototype,'location',{get(){return globalThis.location;}});
})();
