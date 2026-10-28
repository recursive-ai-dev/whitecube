#!/usr/bin/env python3
"""Real broker/renderer/JS integration over isolated HTTP fixtures."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from test_subresources import server, color_count
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
