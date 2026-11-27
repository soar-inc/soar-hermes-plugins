"""Regressions for Meet's in-page caption and capture state."""

import shutil
import subprocess

import pytest


def run_browser_script(source: str, harness: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the mock Meet DOM")
    subprocess.run([node, "-e", harness, source], check=True, timeout=10)


def test_caption_reader_tracks_visible_regions_across_layouts():
    from plugins.google_meet.meet_bot import _CAPTION_OBSERVER_JS

    run_browser_script(_CAPTION_OBSERVER_JS, """
    const assert = require('node:assert/strict');
    global.window = {};
    const region = (innerText, shown = true) => ({
      innerText, shown, children: [],
      getClientRects() { return [{}]; },
      checkVisibility() { return this.shown; },
      querySelectorAll() { return []; },
      contains(other) { return this.children.includes(other); },
    });
    const hidden = region('Hidden captions', false);
    const main = region('First caption');
    const nested = region('Duplicate caption');
    const secondary = region('Separate visible captions');
    main.children.push(nested);

    let tick;
    let mutation;
    global.setInterval = (fn) => { tick = fn; };
    global.MutationObserver = class {
      constructor(fn) { mutation = fn; }
      observe() {}
      disconnect() {}
    };
    global.document = { querySelectorAll: () => [hidden, main, nested, secondary] };
    eval(process.argv[1]);
    const read = () => window.__hermesMeetDrain().map(({ text }) => text);
    assert.deepEqual(read(), ['First caption', 'Separate visible captions']);
    tick();
    assert.deepEqual(read(), []);

    main.shown = false;
    nested.shown = false;
    hidden.shown = true;
    hidden.innerText = 'Captions in a new layout';
    tick();
    assert.deepEqual(read(), ['Captions in a new layout']);

    hidden.innerText = 'Updated caption';
    mutation();
    assert.deepEqual(read(), ['Updated caption']);
    """)


def test_capture_guard_reports_ended_even_when_track_stops_without_an_event():
    from plugins.google_meet.presentation import _CAPTURE_GUARD

    run_browser_script(_CAPTURE_GUARD, """
    const assert = require('node:assert/strict');
    const key = 'mock-tab';
    const track = {
      readyState: 'live',
      getSettings: () => ({ displaySurface: 'browser' }),
      getCaptureHandle: () => ({ handle: key }),
      addEventListener() {},
      stop() { this.readyState = 'ended'; },
    };
    global.window = { get top() { return this; } };
    global.location = { origin: 'https://meet.google.com', pathname: '/test' };
    Object.defineProperty(globalThis, 'navigator', {
      value: { mediaDevices: { getDisplayMedia: async () => ({ getVideoTracks: () => [track] }) } },
    });
    const install = eval(process.argv[1]);
    install({ origin: location.origin, path: location.pathname, key });
    (async () => {
      assert.equal(window.__hermesMeetShare(key, 'arm'), 'armed');
      await navigator.mediaDevices.getDisplayMedia();
      assert.equal(window.__hermesMeetShare(key), 'capturing');
      track.stop();
      assert.equal(window.__hermesMeetShare(key), 'ended');
      assert.equal(window.__hermesMeetShare(key, 'arm'), 'armed');
    })().catch(error => { console.error(error); process.exitCode = 1; });
    """)
