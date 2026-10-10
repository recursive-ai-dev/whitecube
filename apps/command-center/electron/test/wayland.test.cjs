'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { waylandProblem, enforce } = require('../wayland.cjs');

test('refuses without WAYLAND_DISPLAY', () => {
  assert.match(waylandProblem({}, () => true), /WAYLAND_DISPLAY/);
});
test('refuses when socket is missing or session is x11', () => {
  assert.match(waylandProblem({ WAYLAND_DISPLAY: 'wayland-0', XDG_RUNTIME_DIR: '/run/u' }, () => false), /does not exist/);
  assert.match(waylandProblem({ WAYLAND_DISPLAY: 'wayland-0', XDG_RUNTIME_DIR: '/run/u', XDG_SESSION_TYPE: 'x11' }, () => true), /x11/);
});
test('accepts a wayland session', () => {
  assert.equal(waylandProblem({ WAYLAND_DISPLAY: 'wayland-0', XDG_RUNTIME_DIR: '/run/u', XDG_SESSION_TYPE: 'wayland' }, () => true), null);
});
test('enforce forces ozone wayland and hides DISPLAY', () => {
  const sw = {};
  const env = { DISPLAY: ':0' };
  enforce({ commandLine: { appendSwitch: (k, v) => { sw[k] = v; } } }, env);
  assert.equal(sw['ozone-platform'], 'wayland');
  assert.equal(env.DISPLAY, undefined);
  assert.equal(env.GDK_BACKEND, 'wayland');
});
