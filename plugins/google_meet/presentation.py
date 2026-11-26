"""Present an explicitly requested web tab from the running Google Meet browser."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from pathlib import Path
from queue import Empty, SimpleQueue
from urllib.parse import urljoin, urlsplit

from plugins.google_meet.flowmind_session import FlowMindSession, FlowMindSessionError

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
    if (correctPage) {
        Object.defineProperty(window, '__hermesMeetShare', { value: (key, command) => {
            if (key !== cfg.key) return null;
            if (command === 'arm') {
                until = performance.now() + 20000;
                state = 'armed';
            } else if (command === 'disarm') {
                until = 0;
                if (state === 'armed' || state === 'selecting') state = 'cancelled';
            }
            return liveTrack?.readyState === 'live' ? 'capturing' : state;
        }});
    }
    Object.defineProperty(devices, 'getDisplayMedia', { value: async options => {
        if (!correctPage || !until || performance.now() > until)
            throw new DOMException('No authorized dashboard share is pending.', 'NotAllowedError');
        const deadline = until;
        until = 0;
        state = 'selecting';
        try {
            const stream = await capture({ ...options, audio: false });
            const track = stream.getVideoTracks()[0];
            if (state !== 'selecting' || performance.now() > deadline
                || track?.getSettings().displaySurface !== 'browser'
                || track?.getCaptureHandle?.()?.handle !== cfg.key) {
                stream.getTracks().forEach(item => item.stop());
                throw new DOMException('The source was not the authorized dashboard tab.', 'NotAllowedError');
            }
            liveTrack = track;
            state = 'capturing';
            track.addEventListener('ended', () => { state = 'ended'; }, { once: true });
            track.addEventListener('capturehandlechange', () => {
                if (track.getCaptureHandle()?.handle !== cfg.key) {
                    stream.getTracks().forEach(item => item.stop());
                    state = 'ended';
                }
            });
            return stream;
        } catch (error) {
            if (state !== 'cancelled') state = 'capture-rejected';
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


def capture_browser_config(meet_url: str) -> tuple[str, str]:
    url = urlsplit(meet_url)
    key = f"Dev share {secrets.token_hex(12)}"
    config = {"origin": f"{url.scheme}://{url.netloc}", "path": url.path.rstrip("/"), "key": key}
    return key, f"{_CAPTURE_GUARD}({json.dumps(config)})"


class Presentation:
    """Playwright-side controller for one owned tab and one presentation request at a time."""

    def __init__(self, meet_page, state, *, capture_key: str):
        self.meet = meet_page
        self.state = state
        self.tab = None
        self.flowmind_session = None
        self.capture_key = capture_key
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

    def start(self, url: str, title: str) -> None:
        from tools.bot_desktop import lease

        try:
            lease.assert_agent_may_act()
        except lease.HumanHasControl as exc:
            raise PresentationError("Bot Screen is under human control.") from exc
        if self._capture() == "capturing" or _control(self.meet, _STOP):
            raise PresentationError("There's already a presentation running from my Meet tab.")

        flowmind = FlowMindSession.for_url(self.meet.context, url)
        if flowmind:
            try:
                flowmind.ensure()
            except FlowMindSessionError as exc:
                raise PresentationSignInRequired(str(exc)) from exc
            url = flowmind.view_url

        tab = self.meet.context.new_page()
        try:
            navigation_errors = []
            tab.route("**/*", lambda route: _page_route(route, navigation_errors=navigation_errors))
            tab.on("popup", lambda popup: popup.close())
            try:
                response = tab.goto(url, wait_until="domcontentloaded", timeout=15_000)
                if flowmind and response is not None and response.status == 401:
                    flowmind.ensure(force=True)
                    response = tab.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except FlowMindSessionError as exc:
                raise PresentationSignInRequired(str(exc)) from exc
            except Exception as exc:
                error = navigation_errors[-1] if navigation_errors else PresentationError("The requested page couldn't load.")
                raise error from exc
            final_url = _presentation_url(tab.url)
            if flowmind and final_url != flowmind.view_url:
                raise PresentationError("FlowMind redirected away from its read-only dashboard.")
            if (not flowmind and _sign_in_path(final_url)) or (response is not None and response.status == 401):
                raise PresentationSignInRequired("Dev needs a browser sign-in to present this page.")
            if response is None or not response.ok:
                raise PresentationError("The requested page didn't load successfully.")
            tab.evaluate(_TAG_DASHBOARD, {"key": self.capture_key, "origin": "https://meet.google.com"})
            lease.assert_agent_may_act()
            self.meet.bring_to_front()
            present = _control(self.meet, _PRESENT)
            if present is None:
                raise PresentationError("I couldn't identify the Present now button in Google Meet.")
            if self._capture("arm") != "armed":
                raise PresentationError("Couldn't arm the dashboard capture for this call.")
            present.click(timeout=4_000)
        except Exception:
            with suppress(Exception):
                self._capture("disarm")
            with suppress(Exception):
                tab.close()
            raise

        self.tab, self.title, self.url = tab, title, final_url
        self.flowmind_session = flowmind
        self.until = time.monotonic() + 20
        self._phase("verifying-presentation")

    def _dispatch(self) -> None:
        try:
            action, args, result = self.requests.get_nowait()
        except Empty:
            return
        if result.cancelled():
            return
        if action == "stop":
            try:
                result.set_result(self.stop())
            except Exception:
                with suppress(Exception):
                    result.set_result({"ok": False, "error": "Couldn't stop the current presentation."})
            return
        if self.pending is not None or self.phase == "presenting":
            result.set_result({"ok": False, "error": "Stop the current presentation before starting another."})
            return
        self.pending = result
        try:
            url = _presentation_url(args.get("url"))
            title = args.get("title")
            title = " ".join(title.split())[:120] if isinstance(title, str) else ""
            self.start(url, title or urlsplit(url).hostname)
        except PresentationSignInRequired as exc:
            self._failure(str(exc), phase="sign-in-required")
        except PresentationError as exc:
            self._failure(str(exc))
        except Exception:
            self._failure("Couldn't start sharing that tab in Google Meet.")

    def poll(self) -> None:
        self._dispatch()
        if self.pending is not None and self.pending.cancelled():
            self._failure("The presentation request expired.")
            return
        if self.tab is not None and (self.tab.is_closed() or self.tab.url != self.url):
            self._failure("The shared page changed or closed.")
            return
        if self.flowmind_session is not None:
            try:
                self.flowmind_session.maintain()
            except FlowMindSessionError as exc:
                self._failure(str(exc), phase="sign-in-required")
                return
        if self.phase == "verifying-presentation":
            capture = self._capture()
            if capture == "capturing":
                self._phase("presenting")
                self._complete({"ok": True, "state": "presenting", "title": self.title})
            elif capture == "armed" and (tab := _control(self.meet, _TAB, role="menuitem")):
                from tools.bot_desktop import lease
                lease.assert_agent_may_act()
                tab.click(timeout=2_000)
            elif capture == "capture-rejected" or time.monotonic() >= self.until:
                self._failure("Google Meet didn't start capturing the requested tab.")
        elif self.phase == "presenting" and self._capture() != "capturing":
            self._close_tab()

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
        self.flowmind_session = None
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
