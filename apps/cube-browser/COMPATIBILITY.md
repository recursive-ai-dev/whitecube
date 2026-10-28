# Cube Browser compatibility

This records the implemented subset, with deterministic coverage in `tests/`.
The wider target is in `spec.md` and release progress in `SPRINT.md`.

## HTML and layout

The bounded tree parser recovers omitted paragraph, list-item, description-item,
heading, option and table-cell end tags, including intervening inline elements.
It is not a full HTML tree-construction implementation.

Selectors support tag, ID, class, attribute presence and string operators,
descendant/child/adjacent/general sibling relationships, first/last/nth-child
(integer, odd, even), not with compound selectors, link, checked, enabled,
disabled and focus. Escaped identifiers and the complete functional-selector
grammar are not implemented.

Custom properties inherit and resolve before inheritance to children. `var()`
supports nested fallbacks and rejects cyclic expansion; names are case-sensitive.
Each element has at most 64 custom properties, with 4096-byte values, 16 expansion
levels and 512 expansion operations. Existing cascade and important priorities
apply. CSS-wide shorthand resets and the complete computed-value grammar remain
outside the subset.

Row flex supports basis, growth, weighted shrinkage, min/max width, wrapping,
gaps, justification and cross-axis alignment. Column flex supports basis,
growth/shrinkage in a definite height, gaps and alignment. Intrinsic width uses
shaped text; min-content sizing, baseline alignment, reverse directions and
wrapping columns remain deferred. A flex/grid container handles 512 items before
falling back to flow. The home page explicitly uses `flex:1` for equal cards.

Grid supports 1–16 equal `1fr` columns, including `repeat(N,1fr)`, automatic rows,
gaps and content-driven row height. Explicit placement, spanning, subgrid and
intrinsic track algorithms are not implemented.

Block dimensions support width/height and their min/max constraints. The existing
preview keeps border-box sizing by default; explicit `box-sizing:content-box`
adds padding and borders. Relative positioning moves painting and hit geometry
without moving subsequent flow. Absolute positioning uses the nearest positioned
ancestor, supports physical offsets and opposite-edge stretching, and paints
after normal flow. Stacking contexts, z-index, fixed/sticky positioning and
floats remain deferred.

`overflow:hidden` and `overflow:clip` clip both painting and hit targets; fully
clipped controls/links leave keyboard traversal. Nested scrolling is deferred.
Responsive media, bounded tables, images and GET interactions remain covered by
the original suites. Reference semantics: [Flexbox](https://www.w3.org/TR/css-flexbox-1/)
and [Custom Properties](https://www.w3.org/TR/css-variables-1/).

## Isolation and resource policy

Workers fail closed without Linux Landlock ABI 3 and seccomp filter support.
Landlock ABI 6 additionally scopes abstract UNIX sockets and signals. Native
Wayland stays in the UI; only sealed document/resource/frame descriptors cross
the private IPC channel. The network worker can create IPv4 sockets, while the
renderer can create neither sockets nor executable mappings. Font reads use
system directories only; user font directories are not exposed.

HTTP response CSP policies intersect. Supported directives are `default-src`,
`style-src`, `style-src-elem`, `img-src`, `script-src`, `script-src-elem`,
`connect-src`, `form-action`, `style-src-attr` and the script/form restrictions of `sandbox`.
Source expressions include `'self'`, `'none'`, `*`, HTTP(S) scheme/host/port/path
sources and subdomain wildcards. Inline content needs `'unsafe-inline'` when
restricted. Nonces and hashes are not implemented and suppress unsafe-inline;
unsupported authorization stays blocked. This is not full CSP3: meta policies,
reporting and full sandbox opaque-origin semantics are not supported. HTTP(S) resource checks and HTTPS downgrade protection run on
every redirect. Hostnames are ASCII/punycode; IPv6 URLs are not yet supported.

Validated policy references: <https://www.w3.org/TR/CSP3/> and
<https://docs.kernel.org/userspace-api/landlock.html>.

## Forms, sessions and storage

Password inputs are editable, display masks and refuse copy/cut disclosure. They
require POST for submission. GET and POST use UTF-8 URL encoding; POST preserves
the action query and sends at most 16 KiB in a sealed body, never the address or
history. Multipart/file forms remain unsupported. A 303 (and POST 301/302) becomes
GET; 307/308 preserve the body only within the same origin. Reload/back use GET
and do not automatically replay submitted bodies.

The UI owns cookies (256 total, 50 per domain), with host/domain/path matching,
Secure, HttpOnly, expiry, default SameSite=Lax, Strict/None, and __Host-/__Secure-
prefix checks. Third-party subresource cookies are disabled. Domain cookies use
the vendored Public Suffix List, including private domains, wildcard and exception
rules. Snapshot: 2026-10-07, upstream commit
`3929462652695bad04f0a27afb600974014a3c8b`; SHA256
`ba7d836c0ea57a8bcf9f1fcb868066a7554bed029b441ecc6204ad8494a32612`.
The list retains its MPL-2.0 header in `data/public_suffix_list.dat`.
Expired cookies and Max-Age=0 remove sessions. Session cookies belong to the
current window; only explicitly persistent cookies survive reopening. Private
windows start an independent in-memory jar and never write it to the profile.

The memory HTTP cache stores at most 32 validator-bearing responses/16 MiB and
revalidates on every GET with ETag or Last-Modified. Cookie-bearing requests,
Set-Cookie responses, Vary and private/no-store responses bypass storage.
304 bodies are reconstructed only for a matching conditional request. No disk
HTTP cache, speculative freshness or service-worker cache is implemented.

Origin storage is split by scheme/host/effective port. Local storage persists in
owner-only `sites.sqlite3`; session storage stays in this window. Each origin has
128 entries/256 KiB of escaped JSON per area, with at most 64 origins. Keys are
limited to 1024 UTF-8 bytes and values to 16384 bytes. The encoded quota also bounds
startup snapshots when values contain many control characters. Private storage is an independent
in-memory database. The storage layer supports clearing cookies, both storage
areas and the memory cache. Native data clearing also resets site script choices
and the current window's back/forward trail. Local files have no origin storage.

## JavaScript runtime

The replaceable `cube-js` C interface embeds the MIT QuickJS 2026-06-04 interpreter
core. The pinned archive SHA256 is
`b376e839b322978313d929fd20663b11ba58b75df5a46c126dd19ea2fa70ad2a`;
`scripts/bootstrap-quickjs.sh` verifies it and omits `quickjs-libc`. There are no
filesystem/process/network host modules or JIT. Dynamic modules fail explicitly;
SharedArrayBuffer and Atomics are not exposed. The interpreter runs within the
renderer filesystem/syscall restrictions.

Per document: 32 MiB JS heap, 512 KiB JS stack, 1 MiB script/result strings,
100 ms per evaluation/callback/job turn, 256 jobs per checkpoint, and 2 seconds
cumulative execution. Exhausted time/job budgets disable further script execution
for that document. Console output is bounded to 128 entries of 2047 bytes.
Dynamic evaluation can be disabled, including normal/async/generator Function
constructors. Selected language cases, promise jobs, exceptions, runaway code and
allocation limits are tested; this is not a claim that the full Test262 suite has
been run. Upstream API reference: <https://bellard.org/quickjs/quickjs.html>.

## DOM and asynchronous APIs

Pages can use stable Element/Text objects, selectors from the CSS subset,
attributes, text and child mutation, classList/dataset/inline style, input values,
checked state, and document title. EventTarget supports listeners, capture/bubble,
once/passive and default cancellation. Native clicks and keyboard input dispatch
click/key/input/submit/focus events; cancelled submission stays in the document.
Element `matches`/`closest` work on detached nodes. Selector lists return unique
nodes in document order and retain commas inside quoted attributes. Element
sibling traversal and descendant tag/class collections return snapshots. DOM
`textContent` includes hidden descendants and is bounded by the 1 MiB host result
limit, rather than the smaller form-value bound.

`innerHTML` reads and replaces an element's children; `outerHTML` is read-only.
`insertAdjacentHTML` and `insertAdjacentElement` support all four positions.
HTML insertion uses the browser's bounded recovery parser, without the complete
HTML context rules for tables, templates or foreign content. A fragment is limited
to 60,000 UTF-8 bytes. Source, node, depth and control quotas are checked before
changing the live tree; rejected fragments leave existing children intact.
The document retains its 16,000-node, depth-128 and 128-control lifetime limits,
including detached nodes, and a cumulative 4 MiB mutation-source budget. HTML
serialization escapes text/attributes, preserves relative attribute URLs, emits
attribute names in sorted order and rejects output over 128 KiB.

Fragment scripts are inert. Styles/title declarations and external resource
catalogs from fragments are discarded; inserted images do not fetch new resources.
Inline style attributes still use the normal cascade and CSP checks. Inline event
attributes are not evaluated. DOMContentLoaded follows the ordered classic/deferred
script queue. Scripts run after HTML parsing; parser-blocking `document.write`,
modules, dynamic script execution, live collections, iframes, Shadow DOM and full
CSSOM remain unsupported. Async script attributes use the deferred queue.

Promises and function-only timers run within interpreter budgets (64 active timers,
16 timer callbacks per turn). Fetch supports GET with omit/same-origin credentials,
text/JSON responses, CSP connect-src and CORS checks on every response. Fetch and
POST include a serialized Origin header. Redirects that change origin are refused
for fetch; same-origin redirect chains remain supported. Custom request
headers, fetch uploads, streaming, AbortController and XHR are not exposed. There
are at most 32 pending fetches and three active fetch workers; navigation kills
old workers and discards the old runtime. Fetch text preserves embedded NULs and
replaces malformed UTF-8. Its body must fit both a 1 MiB input bound and a 1 MiB
JSON callback bound; larger responses reject the promise. Origin storage exposes the Storage
methods and is initialized from the trusted UI's local/session snapshots.

Scripted navigation is unsupported; native navigation/submission still requires
matching user input. Inline handlers in HTML attributes are not evaluated. CSP
dynamic-code restrictions apply to eval and all Function constructors. A sandbox
policy without allow-same-origin conservatively disables scripts, form submission,
connect requests and origin storage until a complete opaque-origin model exists.
Such pages receive empty storage snapshots and omit cookies on subresource loads.
Attribute style policy now uses style-src-attr separately from style-src-elem.

Origin and CORS references: [URL origin serialization](https://url.spec.whatwg.org/#concept-origin-serializer)
and [Fetch](https://fetch.spec.whatwg.org/#http-requests). This preview implements the
bounded subset described above, rather than the complete Fetch protocol.

Headless rendering waits for script initialization and up to one second of async
settling after initialization, bounded by the overall command deadline. Infinite
intervals cannot keep a headless command alive indefinitely. `--console` prints
bounded page console output to stderr; it does not execute privileged commands.

## Everyday workflows

The Menu button and Ctrl+, open a native settings panel. It controls the default
and current-site JavaScript choices, current-page homepage, zoom and data clearing.
Site choices persist by origin; private-window choices remain in memory. Private
clearing affects that window's site state, while normal clearing also clears
persistent history. Downloads remain user files when browsing data is cleared.

Ctrl+S downloads the current HTTP(S) URL, including addresses whose content cannot
be displayed. Ctrl+J opens download status; Escape cancels an active transfer.
Downloads allow 64 MiB, show received bytes/progress, save only a completed sealed
body and never execute or open a file automatically. The destination is an absolute
`XDG_DOWNLOAD_DIR` environment setting or `~/Downloads`; existing files are retained
with numbered new filenames. Basic Content-Disposition filenames are sanitized;
filename* encoding, resume and a multi-download queue are not implemented.

F12 / Ctrl+Shift+J opens console/network diagnostics. Tab switches the list and
arrow keys scroll. Network entries contain method, status, elapsed time and URL;
request headers and POST bodies are not logged. UI scaling follows Whitecube's
0.5–3.0 preference, limited by available window width; page zoom multiplies it up
to 6x. Fractional Wayland output-scale protocols and IME remain future work.

## Responsive pages

`@media`, `<style media>` and `<link media>` support screen/all media types,
`not`/`only`, comma alternatives and parenthesized `and`/`or` conditions. Width and
height queries use CSS pixels, including `min-`/`max-`, with `px`, `em` and `rem`
lengths. The initial `em`/`rem` size is 16px. Orientation, `prefers-reduced-motion`
and `prefers-contrast` are supported; Whitecube strong contrast maps to `more`.
The viewport excludes browser chrome and accounts for page zoom/interface scale.

Nested media rules retain normal source order and cascade precedence. Unknown media
features do not become true under negation; malformed queries do not match. Query
length, nesting and rule counts are bounded. Range-comparison syntax,
media imports, container queries and a complete CSS grammar remain
outside this subset. The implementation follows the supported portions of
[Media Queries 4](https://www.w3.org/TR/mediaqueries-4/) and
[Media Queries 5 preference features](https://www.w3.org/TR/mediaqueries-5/#mf-user-preferences).

HTML tables support captions, row groups, header cells, cell padding/backgrounds,
borders, spacing and bounded `colspan`/`rowspan`. Columns share the available width;
cell content wraps and rows grow to fit it. Links, anchors and form controls retain
their positions after row sizing. The grid is limited to 128 rows, 16 columns and
1024 cells; spans are limited to 16. Unsupported or excessively narrow structures
fall back to document flow. Intrinsic automatic column sizing, column styling and
full collapsed-border conflict resolution are not implemented; `border-collapse`
currently removes spacing while keeping individual cell borders.

## Static SVG images

External `<img>` resources served as `image/svg+xml` can contain paths (including
curves/arcs), basic shapes, transforms, fills/strokes, per-shape opacity, viewBox
scaling and supported linear/radial gradients. Expat validates the XML, a pinned
[NanoSVG parser](third_party/nanosvg/PIN) processes the allowed subset, and Cairo
rasterizes it to the same bounded surfaces used for PNG/JPEG. No additional
downloads or filesystem reads are permitted by the decoder.

Scripts, event handlers, text, embedded images, external references, DTDs, filters,
masks, clipping, animation and unsupported CSS are rejected with the normal
alt-text fallback. This does not support inline SVG markup or SVG documents as
top-level pages. Vector images are rasterized at their intrinsic size when loaded,
then scaled with the page. XML elements, depth, shapes, coordinates, gradient
stops and estimated painting work have separate limits, alongside the existing
2 MiB/4096-dimension/8-million-pixel image budgets. Full SVG compatibility is still
on the roadmap.
