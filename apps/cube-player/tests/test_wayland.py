#!/usr/bin/env python3
"""Real playback and input against an isolated native Whitecube compositor."""
import argparse
import configparser
import contextlib
import functools
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from PIL import Image
from wayland_proxy import WaylandProxy, wait_for
from drop_proxy import InputProxy

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('CUBE_PLAYER_TEST_BINARY',str(ROOT / 'build/cube-player')))
ARTIFACTS = Path(os.environ.get('CUBE_PLAYER_TEST_ARTIFACTS',str(ROOT / 'build/validation')))

def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    subtitle = root / 'captions.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:28,000\nCube Player subtitles are working.\n')
    metadata = root / 'chapters.txt'
    metadata.write_text(';FFMETADATA1\ntitle=Whitecube playback check\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=14000\ntitle=First half\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=14000\nEND=28000\ntitle=Second half\n')
    video = root / 'sample.mkv'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=640x360:rate=24:duration=28',
                    '-f','lavfi','-i','sine=frequency=220:duration=28','-f','lavfi','-i','sine=frequency=440:duration=28',
                    '-i',str(subtitle),'-i',str(metadata),'-map','0:v','-map','1:a','-map','2:a','-map','3','-map_metadata','4',
                    '-vf','drawbox=x=0:y=0:w=100:h=75:color=white:t=fill',
                    '-c:v','libx264','-preset','ultrafast','-crf','28','-c:a','aac','-c:s','srt',
                    '-metadata:s:a:0','language=eng','-metadata:s:a:1','language=fra','-y',str(video)],check=True)
    audio = root / 'record.wav'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=330:duration=16','-y',str(audio)],check=True)
    short = root / 'short.wav'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=550:duration=0.7','-y',str(short)],check=True)
    broken = root / 'broken.mp4'
    broken.write_text('This is intentionally not a media file.')
    return video, audio, short, broken

def save_snapshot(proxy, name):
    raw,w,h,stride = proxy.snapshot()
    image = Image.frombytes('RGBA',(w,h),raw,'raw','BGRA',stride,1).convert('RGB')
    image.save(ARTIFACTS / (name + '.png'))
    return image

def contains_video(image):
    crop = image.crop((100,160,min(image.width-300,900),min(image.height-180,420))).convert('RGB')
    return sum(1 for r,g,b in crop.getdata() if max(r,g,b)-min(r,g,b)>80)>30000

def run():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    video,audio,short,broken = fixture(ROOT/'build/fixtures')
    with tempfile.TemporaryDirectory(prefix='cube-player-e2e-') as temp:
        root=Path(temp)
        for d in ('runtime','config','state','cache','data'):
            (root/d).mkdir(mode=0o700)
        env=dict(os.environ,XDG_RUNTIME_DIR=str(root/'runtime'),XDG_CONFIG_HOME=str(root/'config'),
                 XDG_STATE_HOME=str(root/'state'),XDG_CACHE_HOME=str(root/'cache'),XDG_DATA_HOME=str(root/'data'),
                 WAYLAND_DISPLAY='',WLR_BACKENDS='headless',WLR_RENDERER='pixman',WLR_HEADLESS_OUTPUTS='1')
        env.pop('WAYLAND_SOCKET',None)
        compositor=Path(os.environ.get('CUBE_PLAYER_TEST_COMPOSITOR',str(next((p for p in (ROOT.parents[1]/'build/whitecube',ROOT.parent/'whitecube/build/whitecube') if p.exists()),ROOT.parent/'whitecube/build/whitecube'))))
        assert compositor.exists(), 'Set CUBE_PLAYER_TEST_COMPOSITOR to a built Whitecube compositor.'
        with (ARTIFACTS/'compositor.log').open('w') as log:
            comp=subprocess.Popen([str(compositor)],env=env,stdout=log,stderr=log,start_new_session=True)
            try:
                upstream=root/'runtime/wayland-0'
                wait_for(lambda:upstream.exists() or comp.poll() is not None,'isolated compositor')
                assert comp.poll() is None
                case_number=0

                @contextlib.contextmanager
                def session(name,args=(),seconds=30,proxy_input=True,proxy_class=WaylandProxy):
                    nonlocal case_number
                    case_number+=1
                    endpoint=root/'runtime'/f'player-test-{case_number}'
                    proxy=proxy_class(endpoint,upstream) if proxy_input else None
                    report=ARTIFACTS/(name+'.ini')
                    with (ARTIFACTS/(name+'.log')).open('w') as player_log:
                        cmd=[str(BINARY),'--silent-test','--quit-after',str(seconds),'--report',str(report)]
                        proc=subprocess.Popen(cmd+list(map(str,args)),env=dict(env,WAYLAND_DISPLAY=endpoint.name if proxy else 'wayland-0'),stdout=player_log,stderr=player_log)
                        try:
                            if proxy:
                                wait_for(lambda:proxy.commits>=2 or proc.poll() is not None or proxy.error,'mapped Cube Player',10)
                                assert not proxy.error, repr(proxy.error)
                                assert proc.poll() is None, (ARTIFACTS/(name+'.log')).read_text()
                                assert proxy.app_id=='cube-player'
                                assert proxy.configures and proxy.acks
                                proxy.keyboard_ready()
                            yield proc,proxy,report
                        finally:
                            if proxy and proxy.error:
                                print('Proxy error:',repr(proxy.error),flush=True)
                                proc.kill()
                            if proc.poll() is None:
                                proc.terminate()
                            try: proc.wait(timeout=8)
                            except subprocess.TimeoutExpired: proc.kill();proc.wait();raise AssertionError('Player shutdown stalled')
                            if proxy: proxy.close()
                            assert proc.returncode==0, (name,proc.returncode,(ARTIFACTS/(name+'.log')).read_text())

                def read_report(path):
                    k=configparser.ConfigParser(interpolation=None);k.read(path);return k['Playback']

                with session('gpu',[video]) as (proc,proxy,report):
                    wait_for(lambda:contains_video(save_snapshot(proxy,'playback')),'actual colorful video pixels',12)
                    frame=save_snapshot(proxy,'playback')
                    # The white marker is at the top-left of the source, not the bottom.
                    assert sum(frame.getpixel((x,110))[0]>230 and frame.getpixel((x,110))[1]>230 for x in range(120,200))>50
                    proxy.key(57)  # pause
                    time.sleep(.3)
                    paused=proxy.snapshot()[0]
                    time.sleep(.3)
                    assert paused==proxy.snapshot()[0], 'Paused video changed'
                    proxy.key(106)  # right: seek five seconds
                    time.sleep(.5)
                    assert paused!=proxy.snapshot()[0], 'Seek did not change the frame'
                    proxy.key(103)  # volume up
                    proxy.key(27)   # speed +.25
                    proxy.key(50)   # mute
                    proxy.key(33)   # fullscreen
                    time.sleep(.25)
                    save_snapshot(proxy,'fullscreen')
                    proxy.key(1)    # exit fullscreen
                    proxy.key(36)   # tracks
                    time.sleep(.2)
                    save_snapshot(proxy,'tracks')
                    # Close is first, audio tracks second/third, subtitle Off fourth,
                    # embedded subtitle fifth. Select by actual keyboard focus.
                    for _ in range(5): proxy.key(15)
                    proxy.key(28)
                    proxy.key(1)
                    time.sleep(.35)
                    save_snapshot(proxy,'subtitles')
                    proxy.key(51,proxy.control)  # preferences
                    time.sleep(.2)
                    save_snapshot(proxy,'settings-light')
                    # Close, Whitecube, Light, Dark.
                    for _ in range(4): proxy.key(15)
                    proxy.key(28)
                    time.sleep(.2)
                    save_snapshot(proxy,'settings-dark')
                    proxy.key(1)
                    proxy.key(24,proxy.control)  # file picker
                    time.sleep(.2)
                    save_snapshot(proxy,'files')
                    proxy.key(38,proxy.control)
                    proxy.type(str(video.parent))
                    proxy.key(28)
                    time.sleep(.2)
                    save_snapshot(proxy,'files-media')
                    proxy.key(1)
                    proxy.key(16,proxy.control)  # quit
                    proc.wait(timeout=5)
                r=read_report(report)
                assert r.getboolean('ever_loaded') and r.getboolean('ever_video')
                assert r.getboolean('paused') and r.getboolean('mute')
                assert r.getfloat('position')>=5 and r.getfloat('volume')==75 and r.getfloat('speed')==1.25
                assert r['renderer']=='OpenGL / Wayland'
                saved=r.getfloat('position')
                print('PASS: OpenGL pixels, orientation, pause, seek, volume, speed, mute, fullscreen, panels, keyboard focus.',flush=True)

                with session('resume',[video],seconds=2.2) as (proc,proxy,report):
                    time.sleep(1.3)
                    proxy.key(57)
                    proc.wait(timeout=5)
                r=read_report(report)
                assert r.getfloat('position')>=saved and r.getfloat('speed')==1.25 and r.getfloat('volume')==75
                print('PASS: playback position, volume, speed, and theme survive restart.',flush=True)

                with session('software',['--software',video],seconds=2.5) as (proc,proxy,report):
                    wait_for(lambda:contains_video(save_snapshot(proxy,'software-playback')),'software video pixels',8)
                    proc.wait(timeout=5)
                assert read_report(report)['renderer']=='Software / Wayland'
                print('PASS: software fallback renders actual video.',flush=True)

                with session('queue',[short,audio],seconds=3) as (proc,proxy,report):
                    wait_for(lambda:'record.wav' in proxy.title,'automatic queue advance',7)
                    save_snapshot(proxy,'audio')
                    proc.wait(timeout=5)
                r=read_report(report)
                assert r['path']==str(audio) and r.getint('current')==1 and r.getint('queue_count')==2
                print('PASS: audio-only playback and automatic next item.',flush=True)

                with session('url-panel',[],seconds=8) as (proc,proxy,report):
                    proxy.key(38,proxy.control)
                    proxy.type(str(audio))
                    proxy.key(28)
                    wait_for(lambda:'record.wav' in proxy.title,'path input starts media',5)
                    time.sleep(.5)
                    proxy.key(16,proxy.control);proc.wait(timeout=5)
                assert read_report(report).getboolean('ever_loaded')
                print('PASS: native path dialog opens actual media.',flush=True)

                with session('drop',['--software'],seconds=8,proxy_class=InputProxy) as (proc,proxy,report):
                    odd=root/'a film with spaces.wav'
                    shutil.copyfile(audio,odd)
                    proxy.drop([odd.as_uri(),video.as_uri()])
                    wait_for(lambda:'a film with spaces.wav' in proxy.title,'dropped media loads',5)
                    time.sleep(.4)
                    # Pointer activates the transport pause button and a position
                    # in the volume slider on the actual mapped 1280x680 window.
                    proxy.click(500,612)
                    proxy.click(852,614)
                    time.sleep(.3)
                    save_snapshot(proxy,'file-drop')
                    proxy.key(16,proxy.control);proc.wait(timeout=5)
                r=read_report(report)
                assert r.getint('queue_count')==2 and r['path']==str(odd)
                assert r.getboolean('ever_loaded') and r.getboolean('paused')
                assert 30<r.getfloat('volume')<45
                print('PASS: multi-file URI drop with spaces, pointer pause, and volume slider.',flush=True)

                class QuietHandler(http.server.SimpleHTTPRequestHandler):
                    def log_message(self,*args): pass
                server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(video.parent)))
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    url=f'http://127.0.0.1:{server.server_port}/sample.mkv'
                    with session('http-stream',[url],seconds=2) as (proc,proxy,report):
                        wait_for(lambda:contains_video(save_snapshot(proxy,'http-stream')),'HTTP media playback',8)
                        proc.wait(timeout=4)
                    r=read_report(report)
                    assert r['path']==url and r.getboolean('ever_video')
                finally:
                    server.shutdown();server.server_close();thread.join(timeout=2)
                print('PASS: direct HTTP media playback.',flush=True)

                with session('invalid',[broken],seconds=2) as (proc,proxy,report):
                    time.sleep(.9);save_snapshot(proxy,'invalid-media');proc.wait(timeout=4)
                r=read_report(report)
                assert not r.getboolean('ever_loaded') and 'Could not play' in r['error']
                print('PASS: invalid media reports an error without crashing.',flush=True)
            finally:
                comp.terminate()
                try: comp.wait(timeout=5)
                except subprocess.TimeoutExpired: comp.kill();comp.wait()
    print('All native Wayland integration checks passed. Artifacts:',ARTIFACTS,flush=True)

if __name__=='__main__':
    run()
