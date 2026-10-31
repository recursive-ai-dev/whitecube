#!/usr/bin/env python3
"""Real broker/renderer/JS integration over isolated HTTP fixtures."""
import os
import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from test_subresources import server, color_count, solid_png
ROOT=Path(__file__).resolve().parents[1]
BROWSER=Path(os.environ.get('CUBE_BROWSER_BUILD',ROOT/'build'))/'cube-browser'
class Web(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cube-web-')
        self.root=Path(self.temp.name)
        self.env=dict(os.environ,HOME=str(self.root),XDG_CONFIG_HOME=str(self.root/'config'),XDG_DATA_HOME=str(self.root/'data'),XDG_STATE_HOME=str(self.root/'state'))
    def tearDown(self):self.temp.cleanup()
    def dump(self,url):
        r=subprocess.run([str(BROWSER),'--console','--dump',url],env=self.env,capture_output=True,text=True,timeout=8)
        self.assertEqual(r.returncode,0,r.stderr)
        return r
    def test_file_location_has_encoded_path_and_opaque_origin(self):
        page=self.root/'file with spaces.html'
        page.write_text("<p id=out>initial</p><script>if(location.protocol!=='file:'||location.origin!=='null'||origin!=='null'||!location.pathname.endsWith('/file%20with%20spaces.html')||location.host!==''||location.search!==''||location.hash!=='')throw Error('file location '+location.pathname);document.getElementById('out').textContent='FILE_LOCATION_OK';</script>")
        r=self.dump(page.as_uri());self.assertIn('FILE_LOCATION_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_event_redispatch_legacy_initialization_abort_and_quota_recovery(self):
        html=b'''<p id="out">initial</p><script>
        const target=new EventTarget(),e=document.createEvent('CustomEvent');e.initCustomEvent('test',false,true,42);
        let calls=0;const listener=event=>{calls++;if(event.detail!==42)throw Error('detail');event.stopImmediatePropagation();event.preventDefault();};
        target.addEventListener('test',listener);if(target.dispatchEvent(e)!==false||target.dispatchEvent(e)!==false||calls!==2||e.currentTarget!==null||e.eventPhase!==0)throw Error('redispatch');
        target.removeEventListener('test',listener);e.initEvent('fresh');if(e.defaultPrevented||e.target!==null)throw Error('initialization');
        const controller=new AbortController();target.addEventListener('fresh',()=>calls++,{signal:controller.signal});controller.abort();target.dispatchEvent(e);if(calls!==2)throw Error('listener abort');
        let denied=0;try{target.addEventListener('x',42)}catch(e){denied++;}if(!denied)throw Error('invalid listener');
        const targets=[],callback=()=>{};for(let i=0;i<4096;i++){const t=new EventTarget();t.addEventListener('x',callback);targets.push(t);}
        for(let i=0;i<2;i++){try{target.addEventListener('x',callback)}catch(e){denied++;}}
        targets[0].removeEventListener('x',callback);target.addEventListener('x',callback);target.removeEventListener('x',callback);
        for(const t of targets)t.removeEventListener('x',callback);
        if(denied!==3)throw Error('quota');
        for(const name of ['unknown','constructor','__proto__','toString'])if(new DOMException('',name).code!==0)throw Error('exception code '+name);
        document.getElementById('out').textContent='EVENT_LIFECYCLE_OK';
        </script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('EVENT_LIFECYCLE_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_media_polling_does_not_exhaust_lifetime_and_images_have_own_interface(self):
        html=b'''<div id="plain"></div><img id="image" width="120" height="80"><p id="out">initial</p><script>
        for(let i=0;i<1000;i++)if(!matchMedia('(pointer:fine)').matches)throw Error('desktop pointer');
        const lists=[];for(let i=0;i<128;i++){const q=matchMedia('(hover:hover)');q.onchange=()=>{};lists.push(q);}
        let denied=false;try{matchMedia('screen').onchange=()=>{}}catch(e){denied=true}if(!denied)throw Error('active quota');
        for(const q of lists)q.onchange=null;matchMedia('screen').onchange=()=>{};
        if(!matchMedia('(prefers-color-scheme:light) and (resolution:1dppx)').matches)throw Error('media features');
        const image=document.getElementById('image'),plain=document.getElementById('plain'),created=new Image(40,30);
        if(!(image instanceof HTMLImageElement)||!(image instanceof Image)||!(image instanceof HTMLElement)||plain instanceof Image||plain.complete!==undefined)throw Error('image interface');
        if(created.width!==40||created.height!==30||created.currentSrc!==''||image.width!==120||image.height!==80)throw Error('image dimensions');
        image.style.width='50%';const width=getComputedStyle(image).width;
        if(!width.endsWith('px')||Math.abs(parseFloat(width)-image.getBoundingClientRect().width)>.01)throw Error('resolved image width');
        if(!(image.cloneNode() instanceof HTMLImageElement))throw Error('clone prototype');
        document.getElementById('out').textContent='MEDIA_IMAGE_INTERFACE_OK';
        </script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('MEDIA_IMAGE_INTERFACE_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_replacement_is_atomic_and_clones_have_independent_inert_state(self):
        html=b'''<div id="parent"><p id="a" data-value="old"><b>child</b><img src="/image.png"></p><p id="b">second</p></div><p id="out">initial</p><script>
        const parent=document.getElementById('parent'),a=document.getElementById('a'),b=document.getElementById('b');
        if(parent.replaceChild(a,a)!==a||parent.firstChild!==a)throw Error('self replacement');
        try{parent.replaceChild(parent,a);throw Error('cycle accepted')}catch(e){if(a.parentNode!==parent)throw Error('failed replacement mutated');}
        const clone=a.cloneNode(true),shallow=a.cloneNode();
        if(clone.isConnected||clone.firstChild===a.firstChild||clone.textContent!=='child'||shallow.childNodes.length)throw Error('clone structure');
        clone.setAttribute('data-value','new');clone.firstChild.textContent='copy';
        if(a.dataset.value!=='old'||a.firstChild.textContent!=='child')throw Error('clone sharing');
        let calls=0;a.addEventListener('test',()=>calls++);clone.dispatchEvent(new Event('test'));if(calls)throw Error('cloned listeners');
        if(parent.replaceChild(clone,b)!==b||b.isConnected||parent.lastChild!==clone)throw Error('replacement');
        document.getElementById('out').textContent='CLONE_REPLACE_OK';
        </script>'''
        with server({'/page':(200,'text/html',html,0),'/image.png':(200,'image/png',solid_png(1,2,3),0)}) as (url,requests,__):
            r=self.dump(url+'/page');self.assertIn('CLONE_REPLACE_OK',r.stdout);self.assertNotIn('console:',r.stderr);self.assertEqual(requests.count('/image.png'),1)
    def test_computed_styles_are_live_readonly_and_use_the_cascade(self):
        html=b'''<style>body{color:#123456;font-size:20px;--size:42px}#box{width:var(--size);padding:2em;display:block}</style>
        <div id="box">box</div><p id="out">initial</p><script>
        const box=document.getElementById('box'),style=getComputedStyle(box);
        if(!(style instanceof CSSStyleDeclaration)||style.color!=='rgb(18, 52, 86)'||style.width!=='42px'||style.paddingLeft!=='40px')throw Error('cascade '+style.width+' '+style.paddingLeft);
        for(const name of ['unknown','constructor','__proto__','toString'])if(style.getPropertyValue(name)!=='')throw Error('unsupported style '+name);
        if(![...style].includes('display')||style[0]!==style.item(0))throw Error('enumeration');
        box.style.width='67px';if(style.width!=='67px')throw Error('not live');
        let denied=0;for(const mutate of [()=>style.width='1px',()=>style.setProperty('color','red'),()=>delete style.color,()=>Object.defineProperty(style,'width',{value:'1px'})]){try{mutate()}catch(e){if(e.name==='NoModificationAllowedError'&&e.code===7)denied++;}}
        if(denied!==4||style.width!=='67px'||getComputedStyle(document.createElement('div')).length!==0)throw Error('read only');
        try{getComputedStyle(box,'::before');throw Error('pseudo accepted')}catch(e){if(e.name!=='NotSupportedError')throw e;}
        document.getElementById('out').textContent='COMPUTED_STYLE_OK';
        </script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('COMPUTED_STYLE_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_navigator_and_location_metadata_support_browser_detection(self):
        html=b'''<p id="out">initial</p><script>
        if(!(navigator instanceof Navigator)||navigator!==window.navigator||navigator!==clientInformation)throw Error('navigator identity');
        for(const key of ['userAgent','appVersion','appName','appCodeName','platform','product','productSub','vendor','vendorSub','language']){
          if(typeof navigator[key]!=='string')throw Error('missing navigator '+key);
          navigator[key].match(/.*/);
        }
        if(!navigator.userAgent.includes('cube-browser/')||!navigator.languages.includes(navigator.language))throw Error('identity metadata');
        let blocked=false;try{new Navigator()}catch(e){blocked=true}if(!blocked)throw Error('navigator constructor');
        if(location.host!==location.hostname+':'+location.port||location.origin!==location.protocol+'//'+location.host||origin!==location.origin)throw Error('location authority');
        if(document.location!==location||location.pathname!=='/page'||location.search!=='?q=1'||location.hash!=='#test')throw Error('location parts');
        if(navigator.serviceWorker!==undefined||navigator.clipboard!==undefined)throw Error('unsupported capabilities');
        document.getElementById('out').textContent='METADATA_OK '+navigator.userAgent;
        </script>'''
        with server({'/page?q=1':(200,'text/html',html,0)}) as (url,_,stats):
            r=self.dump(url+'/page?q=1#test');self.assertIn('METADATA_OK '+stats['headers'][0][1]['User-Agent'],r.stdout)
            self.assertNotIn('console:',r.stderr)
    def test_data_scripts_remain_readable_without_consuming_execution_slots(self):
        data=''.join(f'<script id="data{i}" type="application/json" src="/never.js">{{"value":{i}}}</script>' for i in range(80))
        html=(data+'''<script type="application/ld+json">{"name":"data"}</script>
        <script type="text/template"><p>template</p></script>
        <p id="out">initial</p><script>
        if(document.querySelectorAll('script').length!==83||JSON.parse(document.getElementById('data79').textContent).value!==79)throw Error('data scripts');
        document.getElementById('out').textContent='DATA_SCRIPTS_OK';
        </script>''').encode()
        with server({'/page':(200,'text/html',html,0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('DATA_SCRIPTS_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(requests,['/page'])
    def test_classic_script_types_and_legacy_response_mimes(self):
        html=b'''<p id="out">initial</p><script>let order='A';</script>
        <script type=" TEXT/JAVASCRIPT " src="/legacy.js"></script>
        <script type="application/x-javascript">order+='C';</script>
        <script language="javascript1.2">order+='D';</script>
        <script type="text/javascript; charset=utf-8">throw Error('data block executed');</script>
        <script language="vbscript">throw Error('other language executed');</script>
        <script>document.getElementById('out').textContent=order;</script>'''
        with server({'/page':(200,'text/html',html,0),'/legacy.js':(200,'APPLICATION/X-JAVASCRIPT; charset=utf-8',b"order+='B';",0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('ABCD',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertIn('/legacy.js',requests)
    def test_module_warning_and_classic_script_limits_have_distinct_reasons(self):
        html=b'''<p id="out">initial</p><script type="module" src="/never.js">throw Error('module executed');</script>
        <script>document.getElementById('out').textContent='CLASSIC_OK';</script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('CLASSIC_OK',r.stdout);self.assertIn('JavaScript modules are unsupported',r.stderr)
            self.assertNotIn('limit exceeded',r.stderr);self.assertEqual(requests,['/page'])
        html=('<p>LIMIT_SURVIVED</p>'+'<script>void 0;</script>'*65).encode()
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('LIMIT_SURVIVED',r.stdout);self.assertIn('Script count limit exceeded',r.stderr)
    def test_script_errors_include_the_source_url_and_stack(self):
        html=b'<p>ERROR_SURVIVED</p><script src="/broken.js"></script>'
        script=b'function broken(){ window.missingFunction(); }\nbroken();'
        with server({'/page':(200,'text/html',html,0),'/broken.js':(200,'text/javascript',script,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('ERROR_SURVIVED',r.stdout)
            self.assertIn('TypeError',r.stderr);self.assertIn(url+'/broken.js:1',r.stderr);self.assertIn('broken',r.stderr)
    def test_inline_byte_limit_does_not_mislabel_or_block_later_scripts(self):
        html=('<p id="out">initial</p><script>/*'+'x'*(1024*1024)+'*/</script>'
              '<script>document.getElementById("out").textContent="BYTE_LIMIT_SURVIVED";</script>').encode()
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('BYTE_LIMIT_SURVIVED',r.stdout)
            self.assertIn('Inline script byte limit exceeded',r.stderr);self.assertNotIn('Unsupported script type',r.stderr)
    def test_non_javascript_response_is_refused_and_later_scripts_run(self):
        html=b'<p id="out">SAFE</p><script src="/not-js"></script><script>document.getElementById("out").textContent+=" AFTER";</script>'
        with server({'/page':(200,'text/html',html,0),'/not-js':(200,'application/json',b'document.getElementById("out").textContent="BAD";',0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('SAFE AFTER',r.stdout);self.assertNotIn('BAD',r.stdout)
    def test_lazy_image_observer_requests_and_paints_the_assigned_source(self):
        html=b'''<body style="margin:0"><img id="image" width="160" height="80" data-src="/lazy.png"><p id="out">initial</p><script>
        const image=document.getElementById('image');
        if(document.currentScript!==document.scripts[0])throw Error('current script');
        if(!matchMedia('screen and (min-width:600px)').matches)throw Error('matchMedia');
        image.onload=()=>{
          if(!image.complete||image.naturalWidth!==8||image.naturalHeight!==4)throw Error('image state');
          document.getElementById('out').textContent='LAZY_IMAGE_OK';
        };
        const observer=new IntersectionObserver((entries,owner)=>{
          for(const entry of entries)if(entry.isIntersecting){image.src=image.dataset.src;owner.unobserve(image);}
        },{rootMargin:'20px',threshold:[0,0.5,1]});
        observer.observe(image);
        </script></body>'''
        with server({'/page':(200,'text/html',html,0),'/lazy.png':(200,'image/png',solid_png((36,147,188)),0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('LAZY_IMAGE_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(requests.count('/lazy.png'),1)
            image=self.root/'lazy.png'
            r=subprocess.run([str(BROWSER),'--console','--render',url+'/page',str(image)],env=self.env,capture_output=True,text=True,timeout=8)
            self.assertEqual(r.returncode,0,r.stderr);self.assertNotIn('console:',r.stderr)
            self.assertGreater(color_count(image,0x2493bc),10000)
    def test_dynamic_images_respect_csp_settings_and_local_network_refusal(self):
        html=b'''<p id="out">initial</p><script>
        const image=new Image(100,80);document.body.append(image);
        image.onerror=()=>document.getElementById('out').textContent='IMAGE_BLOCKED';
        image.onload=()=>document.getElementById('out').textContent='LEAK';
        image.src='/blocked.png';
        </script>'''
        routes={'/page':(200,'text/html',html,0,{'Content-Security-Policy':"img-src 'none'"}),'/blocked.png':(200,'image/png',solid_png((255,0,0)),0)}
        with server(routes) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('IMAGE_BLOCKED',r.stdout);self.assertEqual(requests,['/page'])
        config=self.root/'config/cube-browser/config';config.parent.mkdir(parents=True);config.write_text('images=false\n')
        routes['/page']=(200,'text/html',html,0)
        with server(routes) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('IMAGE_BLOCKED',r.stdout);self.assertEqual(requests,['/page'])
        with server({}) as (url,requests,_):
            page=self.root/'local-images.html';page.write_text(f'<p>LOCAL_SAFE</p><script>const image=new Image();image.src="{url}/forbidden.png";document.body.append(image);</script>')
            r=self.dump(str(page));self.assertIn('LOCAL_SAFE',r.stdout);self.assertEqual(requests,[])
    def test_animation_frame_callbacks_can_be_cancelled(self):
        html=b'''<p id="out">initial</p><script>
        cancelAnimationFrame(requestAnimationFrame(()=>{throw Error('cancelled frame ran');}));
        requestAnimationFrame(time=>{
          if(typeof time!=='number'||time<0||performance.now()<time)throw Error('frame timestamp');
          document.getElementById('out').textContent='FRAME_CALLBACK_OK';
        });
        </script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('FRAME_CALLBACK_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_dynamic_image_requests_share_the_document_resource_quota(self):
        html=b'''<p id="out">initial</p><script>
        for(let i=0;i<40;i++){const image=new Image();image.src='/image'+i+'.png';document.body.append(image);}
        document.getElementById('out').textContent='IMAGE_QUOTA_SURVIVED';
        </script>'''
        routes={'/page':(200,'text/html',html,0)}
        routes.update({f'/image{i}.png':(200,'image/png',solid_png((36,147,188)),0) for i in range(40)})
        with server(routes) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('IMAGE_QUOTA_SURVIVED',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(len([path for path in requests if path.endswith('.png')]),32)
    def test_cached_image_assignments_deliver_load_without_refetching(self):
        html=b'''<p id="out">initial</p><script>
        const first=new Image();first.onload=()=>{
          const second=new Image();let loads=0;second.onload=()=>{
            if(++loads!==1)throw Error('duplicate cached load');
            if(second.naturalWidth!==8)throw Error('cached dimensions');
            document.getElementById('out').textContent='CACHED_IMAGE_OK';
          };second.setAttribute('src','/image.png');document.body.append(second);
        };first.src='/image.png';
        </script>'''
        with server({'/page':(200,'text/html',html,0),'/image.png':(200,'image/png',solid_png((36,147,188)),0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('CACHED_IMAGE_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(requests.count('/image.png'),1)
    def test_rejected_image_response_dispatches_one_error(self):
        html=b'''<p id="out">initial</p><script>
        const image=new Image();let errors=0;
        image.onerror=()=>{errors++;setTimeout(()=>document.getElementById('out').textContent=errors===1?'SINGLE_IMAGE_ERROR':'DUPLICATE_IMAGE_ERROR',40);};
        image.src='/malformed.png';
        </script>'''
        with server({'/page':(200,'text/html',html,0),'/malformed.png':(200,'image/png',b'not a PNG',0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('SINGLE_IMAGE_ERROR',r.stdout);self.assertNotIn('DUPLICATE',r.stdout)
            self.assertEqual(requests.count('/malformed.png'),1)
    def test_abort_controller_reasons_events_and_fetch_quota_recovery(self):
        html=b'''<p id="out">initial</p><script>
        const reason={message:'cancelled'},promises=[];
        for(let i=0;i<64;i++){
          const controller=new AbortController();let events=0;
          controller.signal.addEventListener('abort',e=>{events++;e.stopImmediatePropagation();});
          const promise=fetch('/cancel'+i,{signal:controller.signal}).then(()=>{throw Error('cancelled fetch resolved');},value=>{
            if(value!==reason||events!==1||!controller.signal.aborted)throw Error('abort state');
            try{controller.signal.throwIfAborted();throw Error('missing abort reason');}catch(error){if(error!==reason)throw error;}
          });
          controller.abort(reason);controller.abort('ignored');promises.push(promise);
        }
        Promise.all(promises).then(()=>fetch('/fast')).then(r=>r.text()).then(text=>document.getElementById('out').textContent='ABORT_RECOVERED '+text);
        </script>'''
        with server({'/page':(200,'text/html',html,0),'/fast':(200,'text/plain',b'FAST_OK',0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('ABORT_RECOVERED FAST_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            # A request may reach the server before its cancellation message;
            # cancellation must still reject it and recover the native slot.
            self.assertEqual(requests.count('/fast'),1)
    def test_preaborted_fetch_never_starts_and_preserves_falsy_reason(self):
        html=b'''<p id="out">initial</p><script>
        const signal=AbortSignal.abort(0);let events=0;signal.addEventListener('abort',()=>events++);
        fetch('/never',{signal}).then(()=>{throw Error('preabort resolved');},reason=>{
          if(reason!==0||signal.reason!==0||events!==0)throw Error('preabort state');
          document.getElementById('out').textContent='PREABORT_OK';
        });
        </script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('PREABORT_OK',r.stdout);self.assertEqual(requests,['/page']);self.assertNotIn('console:',r.stderr)
    def test_timeout_signal_cancels_an_active_fetch_and_recovers(self):
        html=b'''<p id="out">initial</p><script>
        fetch('/slow',{signal:AbortSignal.timeout(50)}).then(()=>{throw Error('timeout resolved');},reason=>{
          if(!(reason instanceof DOMException)||reason.name!=='TimeoutError'||reason.code!==23)throw Error('timeout reason');
          return fetch('/fast');
        }).then(r=>r.text()).then(text=>document.getElementById('out').textContent='TIMEOUT_CANCELLED '+text);
        </script>'''
        with server({'/page':(200,'text/html',html,0),'/slow':(200,'text/plain',b'late',.5),'/fast':(200,'text/plain',b'FAST_OK',0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('TIMEOUT_CANCELLED FAST_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertIn('/slow',requests);self.assertIn('/fast',requests)
    def test_image_generations_suppress_stale_events_and_settle_invalid_sources(self):
        data='data:image/png;base64,'+base64.b64encode(solid_png((36,147,188))).decode()
        html=('''<p id="out">initial</p><script>
        const good='''+repr(data)+''';let loads=0,errors=0,removedEvents=0;
        const image=new Image();image.onload=()=>loads++;image.onerror=()=>errors++;
        image.src=good;image.src='data:image/png;base64,broken';image.src=good;
        if(!image.complete||image.naturalWidth!==8)throw Error('data image state');
        const removed=new Image();removed.onload=removed.onerror=()=>removedEvents++;
        removed.src=good;removed.removeAttribute('src');
        const invalid=new Image();invalid.onerror=()=>errors++;
        invalid.src='javascript:forbidden';
        if(!invalid.complete||invalid.naturalWidth!==0)throw Error('invalid image state');
        setTimeout(()=>{
          if(loads!==1||errors!==1||removedEvents!==0||!removed.complete)throw Error('image generations '+[loads,errors,removedEvents]);
          document.getElementById('out').textContent='IMAGE_GENERATIONS_OK';
        },50);
        </script>''').encode()
        with server({'/page':(200,'text/html',html,0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('IMAGE_GENERATIONS_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(requests,['/page'])
    def test_webp_data_and_script_assigned_images(self):
        body=(ROOT/'tests/fixtures/solid.webp').read_bytes()
        data='data:image/webp;base64,'+base64.b64encode(body).decode()
        html=('''<p id="out">initial</p><script>
        const image=new Image();let loads=0;image.onerror=()=>{throw Error('WebP failed');};
        image.onload=()=>{
          if(image.naturalWidth!==8||image.naturalHeight!==4||!image.complete)throw Error('WebP dimensions');
          if(++loads===1)image.src='/solid.webp';else document.getElementById('out').textContent='WEBP_IMAGE_OK';
        };image.src='''+repr(data)+''';document.body.append(image);
        </script>''').encode()
        with server({'/page':(200,'text/html',html,0),'/solid.webp':(200,'image/webp',body,0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('WEBP_IMAGE_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(requests.count('/solid.webp'),1)
    def test_observer_options_and_target_limits(self):
        html=b'''<p id="out">initial</p><script>
        let rejected=0;
        for(const options of [{threshold:-1},{threshold:NaN},{threshold:[0,2]},{threshold:new Array(2)},{rootMargin:'1em'},{rootMargin:'1px 2px 3px 4px 5px'}]){
          try{new IntersectionObserver(()=>{},options);}catch(e){rejected++;}
        }
        const observer=new IntersectionObserver(()=>{}),targets=[];
        for(let i=0;i<1024;i++){const target=document.createElement('img');targets.push(target);observer.observe(target);}
        try{observer.observe(document.createElement('img'));}catch(e){rejected++;}
        observer.disconnect();observer.observe(targets[0]);observer.unobserve(targets[0]);
        if(rejected!==7||observer.takeRecords().length)throw Error('observer bounds');
        document.getElementById('out').textContent='OBSERVER_LIMITS_OK';
        </script>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('OBSERVER_LIMITS_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_dom_identity_children_attributes_style_and_event_order(self):
        html='''<body><div id="out">initial</div><script>
        const out=document.getElementById('out');
        if(out!==document.querySelector('#out'))throw Error('identity');
        const p=document.createElement('p'),text=document.createTextNode('hello');p.appendChild(text);out.append(p);
        if(text.parentNode!==p||p.firstChild!==text||!out.contains(text))throw Error('tree');
        p.dataset.example='yes';p.classList.add('a','b');if(document.querySelector('[data-example="yes"]').id!=='')throw Error('attrs');
        p.style.backgroundColor='#2468ac';p.style.minHeight='180px';
        text.data='mutated';const removed=out.removeChild(p);out.appendChild(removed);
        let events='';out.addEventListener('custom',()=>events+='capture',true);p.addEventListener('custom',e=>{events+='target';e.preventDefault()});out.addEventListener('custom',()=>events+='bubble');
        if(p.dispatchEvent(new CustomEvent('custom',{bubbles:true,cancelable:true})))throw Error('cancellation');
        document.addEventListener('DOMContentLoaded',()=>{p.textContent='DOM_OK '+events;document.title='DOM tested'});
        </script></body>'''
        with server({'/page':(200,'text/html',html.encode(),0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('DOM_OK capturetargetbubble',r.stdout);self.assertNotIn('console:',r.stderr)
            image=self.root/'dom.png';r=subprocess.run([str(BROWSER),'--render',url+'/page',str(image)],env=self.env,capture_output=True,text=True,timeout=8)
            self.assertEqual(r.returncode,0,r.stderr);self.assertGreater(color_count(image,0x2468ac),100000)
    def test_script_order_defer_promises_timers_and_fetch_json(self):
        html=b'''<body><p id="out">initial</p><script>let order='A';</script><script defer src="/defer.js"></script><script src="/blocking.js"></script><script>
        order+='C';document.addEventListener('DOMContentLoaded',()=>{order+='E';Promise.resolve().then(()=>{order+='P';setTimeout(async()=>{const r=await fetch('/api');const json=await r.json();document.getElementById('out').textContent=order+' '+json.message;},20);});});
        </script></body>'''
        routes={'/page':(200,'text/html',html,0),'/blocking.js':(200,'text/javascript',b"order+='B';",.05),'/defer.js':(200,'application/javascript',b"order+='D';",0),'/api':(200,'application/json',b'{"message":"FETCH_OK"}',0)}
        with server(routes) as (url,requests,stats):
            r=self.dump(url+'/page');self.assertIn('ABCDEP FETCH_OK',r.stdout);self.assertIn('/api',requests)
            self.assertEqual(next(h['Origin'] for path,h in stats['headers'] if path=='/api'),url)
        self.assertFalse((self.root/'data').exists())
    def test_html_fragments_serialize_replace_and_keep_scripts_resources_inert(self):
        html=b'''<body><div id="out"><b id="old">old</b></div><p id="status">initial</p><script>
        const out=document.getElementById('out'),old=document.getElementById('old');
        out.innerHTML='<a id="link" href="/relative" title="&quot;&amp;&lt;">A &amp; B</a><input id="edit" value="ready">';
        if(old.isConnected||old.textContent!=='old')throw Error('removed subtree');
        if(out.innerHTML!=='<a href="/relative" id="link" title="&quot;&amp;&lt;">A &amp; B</a><input id="edit" value="ready">')throw Error('serialization '+out.innerHTML);
        const link=out.firstElementChild;
        link.insertAdjacentHTML('beforebegin','<span id="before">before</span>');
        link.insertAdjacentHTML('afterend','<span id="after">after</span>');
        link.insertAdjacentHTML('afterbegin','<b>first</b>');
        link.insertAdjacentHTML('beforeend','<i>last</i>');
        if(link.textContent!=='firstA & Blast'||link.previousElementSibling.id!=='before'||link.nextElementSibling.id!=='after')throw Error('positions');
        document.getElementById('edit').value='changed';
        if(document.getElementById('edit').value!=='changed')throw Error('fragment control');
        out.insertAdjacentHTML('beforeend','<script src="/never-script.js">document.title="BAD";<'+ '/script><img src="/never-image.png"><link rel="stylesheet" href="/never.css">');
        if(document.title==='BAD')throw Error('inert script');
        document.getElementById('status').textContent='FRAGMENTS_OK';
        </script></body>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('FRAGMENTS_OK',r.stdout);self.assertNotIn('console:',r.stderr)
            self.assertEqual(requests,['/page'])
    def test_fragment_quotas_are_atomic_and_selectors_work_on_detached_nodes(self):
        html=b'''<body><div id="out">safe</div><p id="first" class="same"></p><p id="second" class="same" data-list="a,b"></p><script>
        const out=document.getElementById('out');let blocked=0;
        for(const value of ['x'.repeat(60001),'<div>'.repeat(130)+'deep'+'</div>'.repeat(130)]){
          try{out.innerHTML=value;}catch(e){blocked++;}
          if(out.textContent!=='safe')throw Error('failed operation mutated document');
        }
        const detached=document.createElement('section');detached.className='chosen';
        if(!detached.matches('section.chosen')||detached.closest('.chosen')!==detached)throw Error('detached matching');
        const ordered=document.querySelectorAll('#second,#first,.same');
        if(ordered.length!==2||ordered[0].id!=='first'||ordered[1].id!=='second')throw Error('selector order');
        if(document.querySelector('[data-list="a,b"]')!==ordered[1])throw Error('quoted comma');
        if(blocked!==2)throw Error('missing quota');
        out.textContent='FRAGMENT_LIMITS_OK';
        </script></body>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('FRAGMENT_LIMITS_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_fragment_control_capacity_and_serialization_bounds(self):
        html=b'''<body><div id="out">safe</div><div id="large"></div><p id="status">initial</p><script>
        const out=document.getElementById('out'),large=document.getElementById('large');
        out.innerHTML='<input value="safe">'.repeat(128);
        const first=out.firstChild;let blocked=0;
        try{out.innerHTML='<input value="replaced">';}catch(e){blocked++;}
        if(out.children.length!==128||out.firstChild!==first||first.value!=='safe')throw Error('control quota changed children');
        for(let i=0;i<3;i++)large.append(document.createTextNode('x'.repeat(60000)));
        const hidden=document.createElement('span');hidden.hidden=true;hidden.textContent='hidden';large.append(hidden);
        try{large.innerHTML;}catch(e){blocked++;}
        if(large.textContent.length!==180006)throw Error('DOM text truncated or skipped hidden nodes');
        large.textContent='';out.textContent='';
        if(blocked!==2)throw Error('missing control or serialization limit');
        document.getElementById('status').textContent='CONTROL_SERIALIZATION_LIMITS_OK';
        </script></body>'''
        with server({'/page':(200,'text/html',html,0)}) as (url,_,__):
            r=self.dump(url+'/page');self.assertIn('CONTROL_SERIALIZATION_LIMITS_OK',r.stdout);self.assertNotIn('console:',r.stderr)
    def test_fetch_origin_redirect_and_text_boundaries(self):
        routes={}
        with server(routes) as (url,requests,stats):
            other_routes={}
            with server(other_routes) as (other,other_requests,other_stats):
                other_routes['/allowed']=(200,'text/plain',b'ORIGIN_OK',0,{'Access-Control-Allow-Origin':url})
                other_routes['/redirect']=(302,'text/plain',(other+'/allowed').encode(),0)
                routes.update({
                    '/bytes':(200,'text/plain',b'a\x00b\xffc',0),
                    '/escaped-limit':(200,'text/plain',b'\x01'*(200*1024),0),
                    '/redirect':(302,'text/plain',(other+'/allowed').encode(),0),
                    '/page':(200,'text/html',f'''<p id="out">initial</p><script>
                    Promise.all([
                      fetch('{other}/allowed').then(r=>r.text()),
                      fetch('/redirect').then(()=> 'LEAK',()=> 'REDIRECT_BLOCKED'),
                      fetch('{other}/redirect').then(()=> 'LEAK',()=> 'CORS_REDIRECT_BLOCKED'),
                      fetch('/bytes').then(r=>r.text()).then(t=>[...t].map(c=>c.charCodeAt(0)).join(',')),
                      fetch('/escaped-limit').then(()=> 'TOO_LARGE',()=> 'LIMIT_REJECTED')
                    ]).then(v=>document.getElementById('out').textContent=v.join(' '));
                    </script>'''.encode(),0)})
                r=self.dump(url+'/page')
                self.assertIn('ORIGIN_OK REDIRECT_BLOCKED CORS_REDIRECT_BLOCKED 97,0,98,65533,99 LIMIT_REJECTED',r.stdout)
                self.assertEqual(other_requests.count('/allowed'),1)
                self.assertTrue(all(h.get('Origin')==url for _,h in other_stats['headers']))
    def test_local_script_refuses_network_and_storage_without_losing_document(self):
        routes={}
        with server(routes) as (url,requests,stats):
            page=self.root/'local.html';page.write_text(f'''<p id="out">initial</p><script>
            let storage='LEAK';try{{localStorage.setItem('secret','value')}}catch(e){{storage='STORAGE_BLOCKED'}};
            fetch('{url}/forbidden').then(()=>document.getElementById('out').textContent='LEAK',()=>document.getElementById('out').textContent=storage+' FETCH_BLOCKED');
            </script>''')
            r=self.dump(str(page));self.assertIn('STORAGE_BLOCKED FETCH_BLOCKED',r.stdout);self.assertEqual(requests,[])
    def test_cors_and_csp_deny_reads_without_stopping_the_document(self):
        with server({'/yes':(200,'application/json',b'{"value":"CORS_OK"}',0,{'Access-Control-Allow-Origin':'*'}),'/no':(200,'text/plain',b'SECRET',0)}) as (other,_,__):
            html=f'''<body><p id="out">initial</p><script>
            Promise.all([fetch('{other}/yes').then(r=>r.json()).then(j=>j.value),fetch('{other}/no').then(()=> 'LEAK',()=> 'BLOCKED')]).then(values=>document.getElementById('out').textContent=values.join(' '));
            </script></body>'''
            with server({'/page':(200,'text/html',html.encode(),0)}) as (url,_,__):
                r=self.dump(url+'/page');self.assertIn('CORS_OK BLOCKED',r.stdout)
        html=b'<body><p id="out">SAFE</p><script>document.getElementById("out").textContent="BAD"</script><script src="/blocked.js"></script></body>'
        with server({'/page':(200,'text/html',html,0,{'Content-Security-Policy':"default-src 'none'"})}) as (url,requests,_):
            r=self.dump(url+'/page');self.assertIn('SAFE',r.stdout);self.assertNotIn('BAD',r.stdout);self.assertEqual(requests,['/page'])
    def test_runaway_script_and_failed_external_script_leave_readable_page(self):
        routes={'/loop':(200,'text/html',b'<p>LOOP_SURVIVED</p><script>while(true){}</script>',0),'/bad':(200,'text/html',b'<p id="out">initial</p><script src="/missing.js"></script><script>document.getElementById("out").textContent="FAILED_SCRIPT_SURVIVED"</script>',0)}
        with server(routes) as (url,_,__):
            r=self.dump(url+'/loop');self.assertIn('LOOP_SURVIVED',r.stdout);self.assertIn('budget exceeded',r.stderr)
            r=self.dump(url+'/bad');self.assertIn('FAILED_SCRIPT_SURVIVED',r.stdout)
if __name__=='__main__':unittest.main()
