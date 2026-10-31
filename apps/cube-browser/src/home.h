#include "version.h"
#ifndef CB_HOME_H
#define CB_HOME_H
static const char cb_home[] =
"<!doctype html><html><head><title>Start here — Cube Browser</title><style>"
"body{background:#fafbf8;color:#18251e;font-family:sans-serif;margin:0;padding:40px;}"
"main{max-width:980px;}h1{font-size:44px;font-weight:700;margin:12px 0 16px;}"
"h2{font-size:20px;margin:0 0 8px;}p{font-size:16px;line-height:1.35;margin:10px 0;}"
".eyebrow{color:#256357;font-size:13px;font-weight:bold;}"
".intro{max-width:720px;color:#4a4a4a;margin-bottom:24px;}"
".row{display:flex;gap:18px;margin:18px 0;}"
".card{flex:1;background:#edf1e9;border:1px solid #cbd5c6;padding:22px;}"
".card p{font-size:14px;color:#4a4a4a;}a{color:#256357;}"
".foot{font-size:13px;color:#4a4a4a;margin-top:32px;}"
".tag{background:#e3ebe1;padding:12px;font-size:13px;}"
"@media(max-width:760px){.row{flex-direction:column;}body{padding:24px;}h1{font-size:34px;}}"
"@media(prefers-contrast:more){body{background:#fff;color:#000;}.card{flex:1;background:#fff;border:2px solid #000;}.intro,.card p,.foot{color:#111111;}}"
"</style></head><body><main>"
"<p class='eyebrow'>WHITE CUBE / AN INDEPENDENT WEB ENGINE</p>"
"<h1>A little window.<br>A wider world.</h1>"
"<p class='intro'>Welcome to Cube Browser. A native home for the simple web, built from the ground up for your Whitecube desktop.</p>"
"<p class='tag'>Press Ctrl+L to enter a web address or search. Tab explores links and form fields. Ctrl+D bookmarks a page. Ctrl+T opens a new desktop tab.</p>"
"<div class='row'><div class='card'><h2>Explore</h2><p>Start with lightweight pages and see the engine at work.</p><p><a href='https://example.com'>Example Domain &#8599;</a></p><p><a href='https://www.debian.org'>Debian &#8599;</a></p></div>"
"<div class='card'><h2>Your trail</h2><p>Keep your favorite pages close. Bookmarks and history stay on this device.</p><p><a href='cube:bookmarks'>Your bookmarks &#8594;</a></p><p><a href='cube:history'>Browsing history &#8594;</a></p></div>"
"<div class='card'><h2>Made here</h2><p>Wayland windows. An original HTML/CSS engine. Software-rendered pages.</p><p><a href='cube:settings'>Browser settings &#8594;</a></p><p><a href='cube:about'>About this build &#8594;</a></p></div></div>"
"<p class='foot'>INTERACTIVE WEB PREVIEW &nbsp; / &nbsp; Responsive pages, basic JavaScript, forms and sign-in sessions are supported. Open Menu for settings, F12 for page diagnostics or Ctrl+J for downloads. Many modern web apps still need APIs this preview does not implement.</p>"
"</main></body></html>";
static const char cb_about[] =
"<html><head><title>About Cube Browser</title></head><body style='padding:48px;max-width:850px;background:#ffffff;color:#111111'>"
"<h1>Cube Browser " CB_VERSION "</h1><p>A ground-up, Wayland-native browser for Whitecube.</p>"
"<h2>Working in this preview</h2><p>Responsive HTML/CSS, flex and simple grids, tables, external stylesheets, PNG/JPEG/static SVG images, direct image viewing, inline data images, GET/POST forms, masked passwords, cookie sessions, local and session storage, bounded JavaScript, DOM events, timers and JSON fetch.</p>"
"<p>Native windows, address/search bar, desktop tabs, back/forward, reload, anchors, scrolling, zoom, find, private browsing, local history, bookmarks, categorized settings, privacy and download preferences, per-site script choices, downloads and console/network diagnostics. HTTP/1.1 uses verified TLS for HTTPS. PNG screenshots and text export use the same engine.</p>"
"<h2>Still to build</h2><p>Complete CSS and DOM support, modules, iframes, file uploads, more image formats, audio/video, WebGL, extensions, HTTP/2 and HTTP/3, IME and advanced Wayland scaling. Complex web apps may render or behave incorrectly.</p>"
"<p>Network, document layout and raster tasks run separately under resource, filesystem and system-call restrictions. This preview has not undergone an independent security audit.</p>"
"<h2>Keyboard</h2><p>Ctrl+L: address &bull; Ctrl+T/W: new desktop tab/close &bull; Alt+Left/Right: back/forward &bull; Ctrl+R: reload &bull; Ctrl+H: history &bull; Ctrl+D: bookmark &bull; Ctrl+Shift+B: bookmarks &bull; Ctrl+Shift+P: private window &bull; Ctrl+F: find &bull; Ctrl+plus/minus/0: zoom &bull; Ctrl+comma: settings &bull; F12: diagnostics &bull; Ctrl+S: save current URL &bull; Ctrl+J: downloads &bull; Escape: cancel download or close panel.</p>"
"<p>Tab/Shift+Tab: next/previous link or control &bull; Enter: activate or submit &bull; Ctrl+A/C/X/V: select/copy/cut/paste in a field. Passwords cannot be copied or cut.</p>"
"<p><a href='cube:home'>Return home</a></p></body></html>";
static const char cb_private_home[] =
"<!doctype html><html><head><title>Private browsing — Cube Browser</title><style>"
"body{background:#f4f1fa;color:#272138;font-family:sans-serif;margin:0;padding:48px;}"
"main{max-width:800px;}h1{font-size:40px;}p{font-size:17px;line-height:1.5;}a{color:#514079;}"
"</style></head><body><main><p>WHITE CUBE / PRIVATE WINDOW</p><h1>A fresh start. A private trail.</h1>"
"<p>This window keeps history, cookies and site storage in memory. Closing it discards that data. Your saved history and settings stay untouched.</p>"
"<p>Press Ctrl+L to open a website or search. Ctrl+T opens another private window.</p>"
"<p><a href='cube:bookmarks'>Read your saved bookmarks</a> · <a href='cube:about'>About this browser</a></p>"
"<p>Downloads remain on disk. Private browsing does not hide your traffic from websites or your network.</p></main></body></html>";
static const char cb_private_history[] =
"<!doctype html><html><head><title>Private history — Cube Browser</title></head>"
"<body style='padding:48px;font-family:sans-serif;background:#f4f1fa;color:#272138'>"
"<h1>Your private trail stays in this window</h1><p>Private browsing never reads or writes your saved browsing history. Use Back and Forward to revisit pages during this session.</p>"
"<p><a href='cube:home'>Private start page</a></p></body></html>";
#endif
