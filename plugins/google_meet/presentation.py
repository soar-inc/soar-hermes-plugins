"""Present an explicitly requested web tab from the running Google Meet browser."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from pathlib import Path
from queue import Empty, SimpleQueue
from urllib.parse import urljoin, urlsplit

_FLOWMIND = "https://build.soar.inc"
_DASHBOARD = f"{_FLOWMIND}/dashboard"
_PRESENT = re.compile(r"^(?:present now|start presenting|share (?:your )?screen)(?:[ (].*\))?$", re.I)
_TAB = re.compile(r"^(?:a tab|chrome tab|chromium tab)$", re.I)
_STOP = re.compile(r"^stop (?:presenting|sharing)(?:[ (].*\))?$", re.I)


class PresentationError(RuntimeError):
    """A short, safe explanation for the bot to say in the meeting."""


class PresentationSignInRequired(PresentationError):
    """The page requires a browser login before it can be shown."""


def bot_screen_env(env: dict[str, str]) -> dict[str, str]:
    from tools.bot_desktop import lease, runtime

    status = runtime.status()
    if sys.platform != "linux" or not status.running or status.placement != "gateway":
        raise PresentationError("Start the profile's local Linux Bot Screen before joining with presentation enabled.")
    try:
        lease.assert_agent_may_act()
    except lease.HumanHasControl as exc:
        raise PresentationError("Hand Bot Screen back to the agent before joining with presentation enabled.") from exc
    child_env = runtime.desktop_env(env)
    if not status.display or child_env.get("DISPLAY") != status.display or not child_env.get("XAUTHORITY"):
        raise PresentationError("Could not attach the Meet browser to this profile's Bot Screen.")
    return child_env


def _is_dashboard(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.netloc == "build.soar.inc"
                and parsed.path.rstrip("/") == "/dashboard" and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def _auth_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "workspace" / "meetings" / "flowmind-auth.json"


def seed_legacy_flowmind(context, profile: Path) -> None:
    """Import the previous cookie file once, only into a profile with no FlowMind cookies."""
    from playwright.sync_api import Error as PlaywrightError

    migrated = profile / ".hermes-flowmind-migrated"
    if migrated.exists():
        return
    if not context.cookies([_FLOWMIND]):
        try:
            saved = json.loads(_auth_path().read_text())
            cookies = [cookie for cookie in saved if cookie["domain"].lstrip(".") == "build.soar.inc"
                       and (cookie["expires"] == -1 or cookie["expires"] > time.time())]
            if cookies:
                context.add_cookies(cookies)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, PlaywrightError):
            # A missing or unusable backup leaves sign-in to Chrome's normal browser flow.
            pass
    migrated.touch(mode=0o600, exist_ok=True)


def authorize_dashboard() -> int:
    from hermes_cli.browser_runtime import chromium_executable
    from playwright.sync_api import sync_playwright
    from plugins.google_meet.chrome_profile import meet_chrome_profile

    try:
        env = bot_screen_env(dict(os.environ))
        with meet_chrome_profile() as profile, sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                profile, channel="chromium", executable_path=chromium_executable(),
                headless=False, env=env, ignore_default_args=["--disable-infobars"],
            )
            try:
                seed_legacy_flowmind(context, profile)
                page = context.pages[0]
                page.goto(_DASHBOARD, wait_until="domcontentloaded", timeout=30_000)
                input("Sign in to FlowMind as Dev in Chrome. Press Enter when the dashboard is visible. ")
                if not _is_dashboard(page.url):
                    raise PresentationError("FlowMind sign-in was not confirmed.")
                response = context.request.get(f"{_FLOWMIND}/api/auth/session", timeout=10_000)
                session = response.json() if response.ok else {}
                if not ((session or {}).get("user") or {}).get("email"):
                    raise PresentationError("FlowMind sign-in was not confirmed.")
            finally:
                context.close()
    except (PresentationError, RuntimeError) as exc:
        print(str(exc))
        return 1
    except Exception as exc:
        print(f"FlowMind sign-in failed ({type(exc).__name__}).")
        return 1
    print("Saved the FlowMind session in the persistent Meet Chrome profile.")
    return 0


def _is_flowmind_origin(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.netloc == "build.soar.inc"
    except (TypeError, ValueError):
        return False


def _presentation_url(url: str) -> str:
    from tools.browser_url_policy import evaluate_url_safety, url_blocked

    try:
        parsed = urlsplit(url)
        valid = (len(url) <= 2_048 and not re.search(r"[\s\\]", url)
                 and parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.port in {None, 443} and parsed.hostname != "meet.google.com")
    except (TypeError, ValueError):
        valid = False
    # FlowMind is configured for this profile but resolves to a private endpoint on the VM.
    flowmind = _is_flowmind_origin(url)
    if (not valid or (not flowmind and url_blocked(url))
            or evaluate_url_safety(url, auto_local=flowmind)):
        raise PresentationError("That URL is not an allowed HTTPS page.")
    return url


def _sign_in_path(url: str) -> bool:
    return bool(re.match(r"^/(?:login|sign-?in|auth|api/auth)(?:/|$)", urlsplit(url).path, re.I))


def _page_route(route, *, navigation_errors: list[PresentationError]) -> None:
    """Check redirects before forwarding a top-level navigation into the shared tab."""
    from tools.browser_url_policy import url_blocked

    request = route.request
    try:
        if not _is_flowmind_origin(request.url) and url_blocked(request.url):
            route.abort()
            return
        if not request.is_navigation_request():
            route.continue_()
            return
        url = request.url
        for _ in range(5):
            _presentation_url(url)
            response = route.fetch(url=url, max_redirects=0, timeout=6_000)
            location = response.headers.get("location")
            if response.status not in {301, 302, 303, 307, 308} or not location:
                route.fulfill(response=response)
                return
            target = urljoin(url, location)
            if _sign_in_path(target):
                navigation_errors.append(PresentationSignInRequired("Dev needs a browser sign-in to present this page."))
                break
            if urlsplit(target).netloc != urlsplit(url).netloc:
                navigation_errors.append(PresentationError("The page redirected to a different site. Ask me to open its final URL."))
                break
            url = target
        route.abort()
    except Exception:
        with suppress(Exception):
            route.abort()


def _unique_visible(locator):
    visible = [item for item in locator.all() if item.is_visible()]
    return visible[0] if len(visible) == 1 else None


def _control(page, name, *, role="button"):
    if button := _unique_visible(page.get_by_role(role, name=name)):
        return button
    # Use the same explicit aria-labels that the Meet admission probe sees.
    if role == "button":
        candidates = page.locator('button[aria-label], [role="button"][aria-label], button[data-tooltip]').all()
        labelled = [item for item in candidates if item.is_visible() and any(
            name.fullmatch((item.get_attribute(attr, timeout=1_000) or "").strip()) for attr in ("aria-label", "data-tooltip")
        )]
        return labelled[0] if len(labelled) == 1 else None
    return None


_CAPTURE_GUARD = r"""
(cfg => {
    const devices = navigator.mediaDevices;
    if (!devices || !devices.getDisplayMedia) return;
    const capture = devices.getDisplayMedia.bind(devices);
    const correctPage = window === window.top && location.origin === cfg.origin
      && location.pathname.replace(/\/$/, '') === cfg.path;
    let until = 0;
    let state = 'idle';
    let liveTrack;
    let navigationTimer;
    let captureGeneration = 0;
    if (correctPage) {
        Object.defineProperty(window, '__hermesMeetShare', { value: (key, command) => {
            if (key !== cfg.key) return null;
            if (command === 'arm') {
                captureGeneration++;
                liveTrack = undefined;
                until = performance.now() + 20000;
                state = 'armed';
            } else if (command === 'begin-navigation') {
                if (navigationTimer !== undefined || liveTrack?.readyState !== 'live'
                    || liveTrack.getCaptureHandle()?.handle !== cfg.key) return 'ended';
                const track = liveTrack;
                track.enabled = false;
                navigationTimer = setTimeout(() => {
                    navigationTimer = undefined;
                    track.stop();
                }, 36000);
                return 'navigation-paused';
            } else if (command === 'finish-navigation') {
                if (liveTrack?.readyState !== 'live' || navigationTimer === undefined) return 'ended';
                if (liveTrack.getCaptureHandle()?.handle !== cfg.key) return 'navigation-paused';
                clearTimeout(navigationTimer);
                navigationTimer = undefined;
                liveTrack.enabled = true;
                return 'capturing';
            } else if (command === 'disarm') {
                captureGeneration++;
                until = 0;
                if (navigationTimer !== undefined) {
                    clearTimeout(navigationTimer);
                    navigationTimer = undefined;
                    liveTrack?.stop();
                }
                if (state === 'armed' || state === 'selecting') state = 'cancelled';
            }
            // Meet can stop its track directly, which does not emit 'ended'.
            if (liveTrack?.readyState === 'live' && navigationTimer !== undefined) return 'navigation-paused';
            if (liveTrack) return liveTrack.readyState === 'live' ? 'capturing' : 'ended';
            return state;
        }});
    }
    Object.defineProperty(devices, 'getDisplayMedia', { value: async options => {
        if (!correctPage || !until || performance.now() > until)
            throw new DOMException('No authorized dashboard share is pending.', 'NotAllowedError');
        const deadline = until;
        const generation = captureGeneration;
        until = 0;
        state = 'selecting';
        try {
            const pickerOptions = cfg.nativePicker
                ? { preferCurrentTab: false, selfBrowserSurface: 'exclude' } : {};
            const stream = await capture({ ...options, ...pickerOptions, audio: false });
            const track = stream.getVideoTracks()[0];
            if (generation !== captureGeneration || state !== 'selecting' || performance.now() > deadline
                || track?.getSettings().displaySurface !== 'browser'
                || track?.getCaptureHandle?.()?.handle !== cfg.key) {
                stream.getTracks().forEach(item => item.stop());
                throw new DOMException('The source was not the authorized dashboard tab.', 'NotAllowedError');
            }
            liveTrack = track;
            state = 'capturing';
            track.addEventListener('capturehandlechange', () => {
                if (navigationTimer === undefined && track.getCaptureHandle()?.handle !== cfg.key) {
                    stream.getTracks().forEach(item => item.stop());
                }
            });
            return stream;
        } catch (error) {
            if (generation === captureGeneration && state !== 'cancelled') state = 'capture-rejected';
            throw error;
        }
    }});
})
"""


_TAG_DASHBOARD = r"""
({key, origin}) => {
    if (!navigator.mediaDevices?.setCaptureHandleConfig)
        throw new Error('Chromium does not expose a tab capture handle.');
    navigator.mediaDevices.setCaptureHandleConfig({ handle: key, permittedOrigins: [origin] });
    const setTitle = () => { if (document.title !== key) document.title = key; };
    setTitle();
    new MutationObserver(setTitle).observe(document.head, { subtree: true, childList: true, characterData: true });
}
"""


def capture_browser_config(meet_url: str, *, native_picker: bool = False) -> tuple[str, str]:
    url = urlsplit(meet_url)
    key = f"Dev share {secrets.token_hex(12)}"
    config = {"origin": f"{url.scheme}://{url.netloc}", "path": url.path.rstrip("/"),
              "key": key, "nativePicker": native_picker}
    return key, f"{_CAPTURE_GUARD}({json.dumps(config)})"


class Presentation:
    """Playwright-side controller for one owned tab and one presentation request at a time."""

    def __init__(self, meet_page, state, *, capture_key: str, native_picker: bool = False):
        self.meet = meet_page
        self.state = state
        self.tab = None
        self.capture_key = capture_key
        self.native_picker = native_picker
        self.picker = None
        self.picker_window = None
        self.picker_maybe_stale = False
        self.picker_retried = False
        self.picker_epoch = None
        self.click_destination = None
        self.navigation_errors = []
        self.title = ""
        self.url = ""
        self.phase = "idle"
        self.until = 0.0
        self.requests = SimpleQueue()
        self.pending = None
        self.closed = threading.Event()

    def request(self, action, args) -> str:
        """Called by the agent worker. Only the browser thread carries out the action."""
        if self.closed.is_set():
            return json.dumps({"ok": False, "error": "Dev is no longer in the call."})
        result = Future()
        self.requests.put((action, args, result))
        try:
            return json.dumps(result.result(timeout=45))
        except TimeoutError:
            result.cancel()
            return json.dumps({"ok": False, "error": "The presentation action timed out."})

    def _phase(self, phase: str, error: str | None = None) -> None:
        self.phase = phase
        self.state.set(presentation_state=phase, presentation_tab=self.title or None, presentation_error=error)

    def _complete(self, result) -> None:
        future, self.pending = self.pending, None
        if future is not None and not future.cancelled():
            with suppress(Exception):
                future.set_result(result)

    def _failure(self, message: str, *, phase: str = "failed") -> None:
        self._close_tab()
        self._phase(phase, message)
        self._complete({"ok": False, "error": message})

    def _capture(self, command=None):
        return self.meet.evaluate(
            "([key, command]) => window.__hermesMeetShare?.(key, command)",
            [self.capture_key, command],
        )

    def _load_page(self, tab, url: str, *, timeout: int = 15000) -> str:
        previous = tab.url
        self.navigation_errors.clear()
        try:
            response = tab.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            error = (self.navigation_errors[-1] if self.navigation_errors
                     else PresentationError("The requested page couldn't load."))
            raise error from exc
        final_url = _presentation_url(tab.url)
        if _sign_in_path(final_url) or (response is not None and response.status == 401):
            raise PresentationSignInRequired("Dev needs a browser sign-in to present this page.")
        if (response is None and final_url == url
                and previous.split("#", 1)[0] == final_url.split("#", 1)[0]):
            return final_url
        if response is None or not response.ok:
            raise PresentationError("The requested page didn't load successfully.")
        return final_url

    @staticmethod
    def _fit_shared_page_width(tab) -> None:
        tab.evaluate("""() => {
            const root = document.documentElement;
            const id = '__hermes-shared-width';
            document.getElementById(id)?.remove();
            const width = root.clientWidth;
            const contentWidth = root.scrollWidth;
            if (!width || contentWidth <= width + 8) return;
            // Responsive pages keep their normal typography. Make a modest adjustment for overflow.
            const style = document.createElement('style');
            style.id = id;
            style.textContent = 'html { zoom: ' + Math.max(0.90, width / contentWidth) + '; }';
            document.head?.appendChild(style);
        }""")

    def _tag_page(self, tab) -> None:
        tab.evaluate(_TAG_DASHBOARD, {"key": self.capture_key, "origin": "https://meet.google.com"})

    def _finish_navigation(self, tab, url: str) -> None:
        self._fit_shared_page_width(tab)
        self._tag_page(tab)
        for _ in range(20):
            if tab.url != url:
                break
            outcome = self._capture("finish-navigation")
            if outcome == "capturing":
                return
            if outcome != "navigation-paused":
                break
            tab.wait_for_timeout(100)
        raise PresentationError("Chrome couldn't confirm the original tab capture.")

    def show_in_shared_tab(self, url: str, title: str, cancelled) -> dict:
        from tools.bot_desktop import lease

        tab = self.tab
        if tab is None or tab.is_closed() or tab.url != self.url or self._capture() != "capturing":
            raise PresentationError("Couldn't verify that the original tab is still sharing.")
        if _sign_in_path(url):
            raise PresentationSignInRequired("Dev needs a browser sign-in to present this page.")
        previous = self.url
        epoch = lease.assert_agent_may_act().epoch
        if cancelled():
            raise PresentationError("The presentation request expired.")
        if url != previous:
            # Chrome temporarily clears a tab's capture handle on document navigation.
            # Mute the existing track until the approved page proves it owns that handle.
            if self._capture("begin-navigation") != "navigation-paused":
                raise PresentationError("Couldn't prepare the original tab for navigation.")
            try:
                destination = self._load_page(tab, url)
                if ((urlsplit(destination).hostname, urlsplit(destination).port or 443)
                        != (urlsplit(url).hostname, urlsplit(url).port or 443) or cancelled()
                        or lease.assert_agent_may_act().epoch != epoch):
                    raise PresentationError("The destination or Bot Screen control changed during navigation.")
                self._finish_navigation(tab, destination)
            except Exception as exc:
                try:
                    if lease.assert_agent_may_act().epoch != epoch:
                        raise PresentationError("Bot Screen control changed.")
                    if tab.url != previous:
                        if self._load_page(tab, previous, timeout=10000) != previous:
                            raise PresentationError("Couldn't restore the previous page.")
                    self._finish_navigation(tab, previous)
                except Exception:
                    self._failure("Couldn't restore the previous page in the shared tab.")
                    raise PresentationError("The new page failed to load and the tab share ended.") from exc
                message = str(exc) if isinstance(exc, PresentationError) else "The requested page couldn't load."
                raise PresentationError(f"{message} The previous page is still shared.") from exc
            self.url = destination
            self.click_destination = None
        self.title = title
        self._phase("presenting")
        return {"ok": True, "state": "presenting", "title": title, "same_tab": True}

    def start(self, url: str, title: str, picker=None) -> None:
        from tools.bot_desktop import lease

        try:
            admitted = lease.assert_agent_may_act()
        except lease.HumanHasControl as exc:
            raise PresentationError("Bot Screen is under human control.") from exc
        if self._capture() == "capturing" or _control(self.meet, _STOP):
            raise PresentationError("There's already a presentation running from my Meet tab.")

        if self.native_picker and picker is None:
            raise PresentationError("Use computer_use to start a Google Meet tab share.")
        if picker is not None and self.meet.context.pages != [self.meet]:
            raise PresentationError("Close other tabs in Dev’s Meet browser before sharing.")
        if picker is not None and self.picker_maybe_stale:
            from plugins.google_meet.picker import active_window

            self.meet.bring_to_front()
            window = active_window(self.meet.title())
            if picker(window, lambda: self.meet.context.pages == [self.meet]
                      and lease.get().epoch == admitted.epoch, dismiss_only=True):
                self.picker_maybe_stale = False

        tab = self.meet.context.new_page()
        try:
            tab.route("**/*", lambda route: _page_route(route, navigation_errors=self.navigation_errors))
            tab.on("popup", lambda popup: popup.close())
            final_url = self._load_page(tab, url)
            if self.native_picker:
                self._fit_shared_page_width(tab)
            self._tag_page(tab)
            lease.assert_agent_may_act()
            window = None
            if picker is not None:
                from plugins.google_meet.picker import PickerError, active_window

                tab.bring_to_front()
                for _ in range(10):
                    try:
                        window = active_window(self.capture_key)
                        break
                    except PickerError:
                        tab.wait_for_timeout(100)
                if window is None:
                    raise PresentationError("Couldn't focus the dashboard tab for the Chrome tab picker.")
            self.meet.bring_to_front()
            if picker is not None and active_window() != window:
                raise PresentationError("Couldn't focus the Meet browser for the Chrome tab picker.")
            if lease.assert_agent_may_act().epoch != admitted.epoch:
                raise PresentationError("Bot Screen control changed before opening the tab chooser.")
            self._open_share_menu(native=picker is not None)
        except Exception:
            with suppress(Exception):
                self._capture("disarm")
            with suppress(Exception):
                tab.close()
            raise

        self.tab, self.title, self.url = tab, title, final_url
        self.picker, self.picker_window = picker, window
        self.picker_retried = False
        self.picker_epoch = admitted.epoch if picker is not None else None
        self.until = time.monotonic() + 20
        self._phase("verifying-presentation")

    def _open_share_menu(self, *, native: bool) -> None:
        from tools.bot_desktop import lease

        admitted = lease.assert_agent_may_act()
        present = _control(self.meet, _PRESENT)
        if native:
            for _ in range(6):
                if present is not None:
                    break
                self.meet.wait_for_timeout(125)
                present = _control(self.meet, _PRESENT)
        if present is None:
            raise PresentationError("I couldn't identify the Present now button in Google Meet.")
        if lease.assert_agent_may_act().epoch != admitted.epoch:
            raise PresentationError("Bot Screen control changed before opening the share menu.")
        if self._capture("arm") != "armed":
            raise PresentationError("Couldn't arm the dashboard capture for this call.")
        if native:
            self.picker_maybe_stale = True
        present.click(timeout=4_000)

    def _retry_dismissed_picker(self, picker) -> None:
        from plugins.google_meet.picker import active_window

        if self.picker_retried:
            raise PresentationError("Chrome's tab chooser was cancelled after the retry.")
        self.picker_retried = True
        for _ in range(10):
            if self._capture() == "capture-rejected":
                break
            self.meet.wait_for_timeout(100)
        else:
            raise PresentationError("Chrome didn't confirm that the stale chooser closed.")
        if (not self._picker_request_active() or active_window() != self.picker_window):
            raise PresentationError("The share request or Meet browser changed before retrying.")
        self.picker_maybe_stale = False
        self._open_share_menu(native=True)
        self.picker = picker

    def _computer_use_target(self) -> dict:
        from tools.bot_desktop import lease

        tab = self.tab
        if (self.phase != "presenting" or tab is None or tab.is_closed()
                or tab.url != self.url or self._capture() != "capturing"):
            raise PresentationError("Dev needs to be sharing a tab before it can interact with the page.")
        try:
            admitted = lease.assert_agent_may_act()
            tab.bring_to_front()
            for _ in range(3):
                active = subprocess.check_output(
                    ["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, timeout=1,
                )
                window = re.search(r"window id # (0x[0-9a-fA-F]+)", active)
                if window and int(window[1], 16):
                    properties = subprocess.check_output(
                        ["xprop", "-id", window[1], "_NET_WM_NAME", "_NET_WM_PID"], text=True, timeout=1,
                    )
                    title = re.search(r'^_NET_WM_NAME\(UTF8_STRING\) = "([^"]+)"$', properties, re.M)
                    pid = re.search(r"^_NET_WM_PID\(CARDINAL\) = ([1-9][0-9]*)$", properties, re.M)
                    if title and pid and title[1].split(" - ", 1)[0] == self.capture_key:
                        if lease.assert_agent_may_act().epoch != admitted.epoch:
                            raise PresentationError("Bot Screen control changed. Please try again.")
                        if tab.evaluate("document.visibilityState === 'visible'") and self._capture() == "capturing":
                            bounds = self._window_bounds(window[1])
                            return {"ok": True, "pid": int(pid[1]), "window_id": int(window[1], 16),
                                    "tab": id(tab), "url": tab.url, "epoch": admitted.epoch, "bounds": list(bounds)}
                tab.wait_for_timeout(100)
        except lease.HumanHasControl as exc:
            raise PresentationError("Bot Screen is under human control.") from exc
        raise PresentationError("Couldn't confirm that Bot Screen is focused on the shared tab.")

    @staticmethod
    def _window_bounds(window: str) -> tuple[int, int, int, int]:
        data = subprocess.check_output(["xwininfo", "-id", window], text=True, timeout=1)
        values = dict(re.findall(r"^\s*(Absolute upper-left [XY]|Width|Height):\s*(-?\d+)\s*$", data, re.M))
        return tuple(int(values[key]) for key in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height"))

    @staticmethod
    def _viewport_screen_origin(tab) -> tuple[float, float, float]:
        pointer = "window.__hermesSharePointer"
        tab.evaluate("""() => {
            window.__hermesSharePointer = null;
            window.addEventListener('pointermove', event => {
                window.__hermesSharePointer = [
                    event.screenX - event.clientX, event.screenY - event.clientY, devicePixelRatio
                ];
            }, {once:true, capture:true});
        }""")
        tab.mouse.move(12, 12)
        tab.mouse.move(16, 16)
        position = tab.evaluate(pointer)
        if not position:
            raise PresentationError("Couldn't measure the shared page's viewport.")
        return tuple(position)

    def _coordinate_hint(self, args) -> dict:
        target = self._computer_use_target()
        if target != args["target"]:
            raise PresentationError("The shared tab or window changed. Capture it again.")
        left, top, width, height = target["bounds"]
        if args["size"] != (width, height):
            raise PresentationError("The screenshot does not match the shared window’s size.")
        tab, label = self.tab, args["label"]
        controls = tab.get_by_role("link", name=label, exact=True).or_(
            tab.get_by_role("button", name=label, exact=True),
        )
        control = _unique_visible(controls)
        box = control.bounding_box(timeout=1000) if control is not None else None
        if box is None:
            return {"ok": False, "error": "No unique visible link or button matched that label."}
        screen_x, screen_y, scale = self._viewport_screen_origin(tab)
        x = round((screen_x + box["x"] + box["width"] / 2) * scale - left)
        y = round((screen_y + box["y"] + box["height"] / 2) * scale - top)
        if not (0 <= x < width and 0 <= y < height):
            return {"ok": False, "error": "That control is outside the captured shared page."}
        return {"ok": True, "label": label, "coordinate": [x, y]}

    def _input_target(self, args, cancelled) -> dict:
        target = self._computer_use_target()
        if target != args["target"] or cancelled():
            raise PresentationError("The shared tab or window changed. Capture it again.")
        return target

    def _verify_input_target(self, target, cancelled) -> None:
        from tools.bot_desktop import lease
        from plugins.google_meet.picker import active_window

        if (cancelled() or self.tab.url != self.url or self._capture() != "capturing"
                or active_window(self.capture_key) != (target["pid"], target["window_id"])
                or self._window_bounds(hex(target["window_id"])) != tuple(target["bounds"])
                or lease.assert_agent_may_act().epoch != target["epoch"]):
            raise PresentationError("The shared tab lost focus or changed before the input.")

    def _coordinate_click(self, args, cancelled) -> dict:
        target = self._input_target(args, cancelled)
        left, top, width, height = target["bounds"]
        if args["size"] != (width, height):
            raise PresentationError("The screenshot does not match the shared window's size.")
        x, y = args["point"]
        if not (0 <= x < width and 0 <= y < height):
            raise PresentationError("That point is outside the shared browser window.")
        tab = self.tab
        label = args["label"]
        screen_x, screen_y, scale = self._viewport_screen_origin(tab)
        hit = tab.evaluate(r"""([x, y, label]) => {
            const node = document.elementFromPoint(x, y);
            const control = node?.closest('a[href], button, [role=button], [role=link]');
            const norm = text => (text ?? '').trim().replace(/\s+/g, ' ').toLowerCase();
            if (!control || control.matches(':disabled, [aria-disabled=true]') ||
                ![control.innerText, control.getAttribute('aria-label')].some(text => norm(text) === norm(label))) {
                return null;
            }
            if (control.hasAttribute('download') || (control.target && control.target !== '_self')) return null;
            if (control instanceof HTMLAnchorElement && new URL(control.href).origin !== location.origin) return null;
            window.__hermesShareClickObserved = false;
            const observe = event => {
                if (event.isTrusted && event.composedPath().includes(control)) {
                    window.__hermesShareClickObserved = true;
                    window.removeEventListener('click', observe, true);
                }
            };
            window.addEventListener('click', observe, true);
            setTimeout(() => window.removeEventListener('click', observe, true), 3000);
            return {href: control instanceof HTMLAnchorElement ? control.href : null};
        }""", [(left + x) / scale - screen_x, (top + y) / scale - screen_y, label])
        if hit is None:
            raise PresentationError("That point doesn't match a visible control on the shared page.")
        destination = _presentation_url(hit["href"]) if hit["href"] else None
        if destination and _sign_in_path(destination):
            raise PresentationError("That link opens a sign-in page.")
        self._verify_input_target(target, cancelled)

        raw = args["click"]({"action": "click", "coordinate": [x, y], "delivery_mode": "foreground"})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": "computer_use couldn't click that shared-page control."}
        # Allow just the destination verified at the point. Existing navigation guards stay in force.
        self.click_destination = (destination, time.monotonic() + 3) if destination else None
        observed = False
        with suppress(Exception):
            observed = tab.evaluate("window.__hermesShareClickObserved === true")
        if not observed and (not destination or tab.url != destination):
            raise PresentationError("The shared page did not confirm the click.")
        return {"ok": True, "action": "click", "label": label}

    def _scroll_shared_tab(self, args, target) -> dict:
        tab = self.tab
        left, top, width, height = target["bounds"]
        origin_x, origin_y, scale = self._viewport_screen_origin(tab)
        point = args["point"]
        if point is None:
            viewport_width, viewport_height = tab.evaluate("() => [innerWidth, innerHeight]")
            point = [round((origin_x + viewport_width / 2) * scale - left),
                     round((origin_y + viewport_height / 2) * scale - top)]
        elif args["size"] != (width, height):
            raise PresentationError("The screenshot doesn't match the shared window's size.")
        x, y = point
        if not (0 <= x < width and 0 <= y < height):
            raise PresentationError("That scroll point is outside the shared browser window.")
        page_point = [(left + x) / scale - origin_x, (top + y) / scale - origin_y]
        if not tab.evaluate("""([x, y]) => {
            if (!document.hasFocus() || !document.elementFromPoint(x, y)) return false;
            window.__hermesSharedWheel = false;
            const observe = event => {
                if (event.isTrusted) window.__hermesSharedWheel = true;
            };
            window.addEventListener('wheel', observe, {once:true, capture:true, passive:true});
            setTimeout(() => window.removeEventListener('wheel', observe, true), 3000);
            return true;
        }""", page_point):
            raise PresentationError("That scroll point isn't inside the shared page.")
        return {"action": "scroll", "coordinate": point, "direction": args["direction"], "amount": args["amount"]}

    def _type_in_shared_tab(self, args) -> dict:
        tab = self.tab
        if label := args.get("label"):
            name = re.compile(r"^" + re.escape(label) + r"$", re.I)
            fields = tab.get_by_role("textbox", name=name).or_(
                tab.get_by_role("searchbox", name=name),
            ).or_(tab.get_by_role("combobox", name=name)).or_(
                tab.get_by_placeholder(name),
            ).or_(tab.get_by_label(name))
            field = _unique_visible(fields)
            if field is None:
                raise PresentationError("Couldn't find one visible text field with that label.")
            if not field.is_editable(timeout=1000):
                raise PresentationError("That field isn't editable.")
            field.focus(timeout=1000)
        if not tab.evaluate("""() => {
            const field = document.activeElement;
            if (!document.hasFocus() || !field || field.matches(':disabled, [readonly], [aria-disabled=true]'))
                return false;
            const textInput = field instanceof HTMLInputElement &&
                ['text', 'search', 'email', 'url', 'tel', 'number'].includes(field.type);
            if (!textInput && !(field instanceof HTMLTextAreaElement) && !field.isContentEditable)
                return false;
            const box = field.getBoundingClientRect();
            const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
            if (!hit || !field.contains(hit)) return false;
            window.__hermesSharedText = false;
            const observe = event => {
                if (event.isTrusted && field.contains(event.target)) window.__hermesSharedText = true;
            };
            window.addEventListener('input', observe, true);
            setTimeout(() => window.removeEventListener('input', observe, true), 3000);
            return true;
        }"""):
            raise PresentationError("Focus a visible editable text field on the shared page before typing.")
        return {"action": "type", "text": args["text"]}

    def _shared_input(self, args, cancelled) -> dict:
        target = self._input_target(args, cancelled)
        action = args["action"]
        if action == "scroll":
            native = self._scroll_shared_tab(args, target)
            observed = "window.__hermesSharedWheel === true"
        else:
            native = self._type_in_shared_tab(args)
            observed = "window.__hermesSharedText === true"
        self._verify_input_target(target, cancelled)
        raw = args["input"]({**native, "delivery_mode": "foreground"})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict) or not result.get("ok"):
            raise PresentationError(f"computer_use couldn't {action} in the shared page.")
        if not self.tab.evaluate(observed):
            raise PresentationError(f"The shared page didn't confirm the {action} input.")
        return {"ok": True, "action": action}

    def _scroll_to_section(self, args, cancelled) -> dict:
        target = self._computer_use_target()
        tab, label = self.tab, args["label"]
        name = re.compile(r"^\s*" + r"\s+".join(map(re.escape, label.split())) + r"\s*$", re.I)
        # Prefer named document sections. Some dashboards use plain labels instead of headings.
        section = _unique_visible(tab.get_by_role("heading", name=name))
        if section is None:
            section = _unique_visible(tab.get_by_role("region", name=name))
        if section is None:
            section = _unique_visible(tab.get_by_text(name))
        if section is None:
            raise PresentationError("Couldn't match one visible section with that name on the shared page.")
        self._verify_input_target(target, cancelled)
        shown = section.evaluate("""node => {
            node.scrollIntoView({behavior: 'instant', block: 'center', inline: 'nearest'});
            const box = node.getBoundingClientRect();
            const x = (Math.max(0, box.left) + Math.min(innerWidth, box.right)) / 2;
            const y = (Math.max(0, box.top) + Math.min(innerHeight, box.bottom)) / 2;
            const hit = document.elementFromPoint(x, y);
            return box.width > 0 && box.height > 0 && hit && node.contains(hit);
        }""", timeout=2000)
        self._verify_input_target(target, cancelled)
        if not shown:
            raise PresentationError("The requested section is still obscured on the shared page.")
        return {"ok": True, "action": "scroll_to_section", "section": label}

    def _dispatch(self) -> None:
        try:
            action, args, result = self.requests.get_nowait()
        except Empty:
            return
        if result.cancelled():
            return
        if action in ("computer-use-target", "computer-use-coordinate", "computer-use-control-point",
                      "computer-use-shared-input", "computer-use-section"):
            try:
                if action == "computer-use-coordinate":
                    target = self._coordinate_click(args, result.cancelled)
                elif action == "computer-use-shared-input":
                    target = self._shared_input(args, result.cancelled)
                elif action == "computer-use-section" and self.native_picker:
                    target = self._scroll_to_section(args, result.cancelled)
                elif action == "computer-use-control-point":
                    target = self._coordinate_hint(args)
                else:
                    target = self._computer_use_target()
            except PresentationError as exc:
                target = {"ok": False, "error": str(exc)}
            except Exception:
                target = {"ok": False, "error": "Couldn't identify the shared browser tab on Bot Screen."}
            if not result.cancelled():
                with suppress(Exception):
                    result.set_result(target)
            return
        if action == "stop":
            try:
                result.set_result(self.stop())
            except Exception:
                with suppress(Exception):
                    result.set_result({"ok": False, "error": "Couldn't stop the current presentation."})
            return
        if self.pending is not None:
            result.set_result({"ok": False, "error": "A tab presentation is already starting."})
            return
        if self.phase == "presenting":
            if action != "computer-use-share" or not self.native_picker:
                result.set_result({"ok": False, "error": "A tab presentation is already active."})
                return
            try:
                url = _presentation_url(args.get("url"))
                title = self._display_title(args.get("title"), url)
                report = self.show_in_shared_tab(url, title, result.cancelled)
            except PresentationError as exc:
                report = {"ok": False, "error": str(exc)}
            except Exception:
                report = {"ok": False, "error": "Couldn't open that URL in the shared tab."}
            if not result.cancelled():
                with suppress(Exception):
                    result.set_result(report)
            return
        self.pending = result
        try:
            url = _presentation_url(args.get("url"))
            picker = args.get("picker") if action == "computer-use-share" else None
            self.start(url, self._display_title(args.get("title"), url), picker=picker)
        except PresentationSignInRequired as exc:
            self._failure(str(exc), phase="sign-in-required")
        except PresentationError as exc:
            self._failure(str(exc))
        except Exception:
            self._failure("Couldn't start sharing that tab in Google Meet.")

    @staticmethod
    def _display_title(title, url: str) -> str:
        normal = " ".join(title.split())[:120] if isinstance(title, str) else ""
        return normal or urlsplit(url).hostname

    def poll(self) -> None:
        self._dispatch()
        if self.pending is not None and self.pending.cancelled():
            self._failure("The presentation request expired.")
            return
        if self.tab is not None and (self.tab.is_closed() or self.tab.url != self.url):
            destination, deadline = self.click_destination or (None, 0)
            if (self.phase == "presenting" and not self.tab.is_closed()
                    and self.tab.url == destination and time.monotonic() <= deadline
                    and self._capture() == "capturing"):
                self.url = destination
                self.click_destination = None
                if self.native_picker:
                    self._fit_shared_page_width(self.tab)
            else:
                self._failure("The shared page changed or closed.")
                return
        if self.phase == "verifying-presentation":
            capture = self._capture()
            if capture == "capturing":
                self.picker_maybe_stale = False
                self._phase("presenting")
                self._complete({"ok": True, "state": "presenting", "title": self.title})
            elif capture == "armed" and (tab := _control(self.meet, _TAB, role="menuitem")):
                from tools.bot_desktop import lease
                lease.assert_agent_may_act()
                tab.click(timeout=2_000)
            elif capture == "selecting" and self.picker is not None and time.monotonic() < self.until:
                from plugins.google_meet.picker import PickerDismissed, PickerError

                picker, self.picker = self.picker, None
                try:
                    picker(self.picker_window, self._picker_pending)
                except PickerDismissed:
                    try:
                        self._retry_dismissed_picker(picker)
                    except PresentationError as exc:
                        self._failure(str(exc))
                    except Exception:
                        self._failure("Couldn't reopen the Chrome tab chooser.")
                except PickerError as exc:
                    self._failure(str(exc))
                except Exception:
                    self._failure("Couldn't operate the Chrome tab picker with computer_use.")
            elif capture == "capture-rejected" or time.monotonic() >= self.until:
                self._failure("Google Meet didn't start capturing the requested tab.")
        elif self.phase == "presenting" and self._capture() != "capturing":
            self._close_tab()

    def _picker_request_active(self) -> bool:
        from tools.bot_desktop import lease

        return (self.pending is not None and not self.pending.cancelled()
                and time.monotonic() < self.until
                and lease.get().epoch == self.picker_epoch
                and self.tab is not None and self.tab.url == self.url
                and set(self.meet.context.pages) == {self.meet, self.tab})

    def _picker_pending(self) -> bool:
        return self._picker_request_active() and self._capture() == "selecting"

    def stop(self) -> dict:
        from tools.bot_desktop import lease

        if self.tab is None and self.pending is None:
            return {"ok": True, "state": "idle"}
        try:
            lease.assert_agent_may_act()
        except lease.HumanHasControl:
            return {"ok": False, "error": "Bot Screen is under human control."}
        try:
            if button := _control(self.meet, _STOP):
                button.click(timeout=2_000)
        finally:
            self._close_tab()
            self._complete({"ok": False, "error": "The presentation request was cancelled."})
        return {"ok": True, "state": "stopped"}

    def _close_tab(self) -> None:
        with suppress(Exception):
            self._capture("disarm")
        if self.tab is not None:
            with suppress(Exception):
                self.tab.close()
        self.tab, self.title, self.url = None, "", ""
        self.picker, self.picker_window = None, None
        self.picker_epoch = None
        self.click_destination = None
        self._phase("idle")

    def close(self) -> None:
        """Cancel presentation requests when the running call ends."""
        self.closed.set()
        self._close_tab()
        error = {"ok": False, "error": "Dev is no longer in the call."}
        self._complete(error)
        while True:
            try:
                _, _, future = self.requests.get_nowait()
            except Empty:
                return
            if not future.cancelled():
                with suppress(Exception):
                    future.set_result(error)
