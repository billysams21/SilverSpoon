import asyncio
import inspect
import json
import logging
import os
import shutil
import sys
import tempfile
import threading

import nodriver
import nodriver.cdp as cdp

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def find_browsers():
    """Return the Playwright Chromium shipped beside the application, or fall back to system browsers during dev."""
    app_dir = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    exe = "chrome.exe" if sys.platform == "win32" else "chrome"
    browser_path = os.path.join(app_dir, "playwright-chromium", exe)
    found = [browser_path] if os.path.isfile(browser_path) else []
    
    # Local Playwright Chromium is the second choice during development.
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", "")
    pf86 = os.environ.get("PROGRAMFILES(X86)", "")
    
    pw_roots = [
        (os.path.join(la, "ms-playwright"), "chrome-win64", "chrome.exe"),
        # Newer Playwright (Chrome for Testing builds) uses chrome-linux64 / chrome-linux-arm64;
        # older releases used chrome-linux.
        *((os.path.expanduser("~/.cache/ms-playwright"), sub, "chrome")
          for sub in ("chrome-linux64", "chrome-linux-arm64", "chrome-linux")),
    ]
    for pw_root, pw_sub, pw_exe in pw_roots:
        if not os.path.isdir(pw_root):
            continue
        for entry in sorted(os.listdir(pw_root), reverse=True):
            if entry.startswith("chromium-") and not entry.startswith("chromium_headless"):
                dev_path = os.path.join(pw_root, entry, pw_sub, pw_exe)
                if os.path.isfile(dev_path):
                    found.append(dev_path)

    # System browser fallbacks
    system_paths = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(la, "Google", "Chrome", "Application", "chrome.exe"),
        shutil.which("chrome"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(la, "Microsoft", "Edge", "Application", "msedge.exe"),
        shutil.which("msedge"),
        *(shutil.which(name) for name in (
            "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
            "brave-browser", "microsoft-edge", "microsoft-edge-stable",
        )),
        "/snap/bin/chromium",
    ]
    
    for path in system_paths:
        if path and os.path.isfile(path) and path not in found:
            found.append(path)
            
    return found


class TurnstileSolver:
    MAX_ATTEMPTS = 2
    # Extra time the user gets once the window is shown for a check that
    # didn't pass on its own (an interactive "Verify you are human").
    USER_SOLVE_TIME = 90

    def __init__(self, profile_dir=None, timeout=10, show_window=False):
        self.TOKEN_TIMEOUT = timeout
        # Off-screen by default. Not minimized: Chrome treats a minimized page
        # as hidden and stops rendering it, which Turnstile can stall on. Takes
        # effect the next time the browser starts.
        self.show_window = show_window
        self._notify = None      # per resolution: notify(waiting_for_user: bool)
        self._revealed = False   # window currently shown for the user
        self._profile_dir = profile_dir or os.path.join(
            tempfile.gettempdir(), "silverspoon_browser_profile"
        )
        self._loop = None
        self._loop_thread = None
        self._browser = None
        self._tab = None
        self._browser_lock = None
        self._user_agent = None
        self._cookies = None
        self._stop_requested = False
        self._init_lock = threading.Lock()

    def _ensure_loop(self):
        with self._init_lock:
            if self._loop is not None:
                return
            ready = threading.Event()
    
            def _run_loop():
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._browser_lock = asyncio.Lock()
                ready.set()
                self._loop.run_forever()
    
            self._loop_thread = threading.Thread(
                target=_run_loop, name="TurnstileSolver-AsyncLoop", daemon=True
            )
            self._loop_thread.start()
            ready.wait(timeout=5)

    def _submit(self, coro):
        self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    async def _close_browser(self):
        if self._browser:
            try:
                logger.info("TurnstileSolver: stopping browser")
                # nodriver's stop() is synchronous (it terminates the process
                # itself); only await it if a version returns a coroutine. The
                # timeout guards against that hanging on a frozen browser.
                result = self._browser.stop()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=3.0)
            except Exception as e:
                logger.error("TurnstileSolver: error stopping browser gracefully: %s", e)
                # Force kill the underlying process if gracefully stopping failed
                try:
                    if hasattr(self._browser, "config") and hasattr(self._browser.config, "browser_process"):
                        proc = self._browser.config.browser_process
                        if proc:
                            proc.kill()
                            logger.info("TurnstileSolver: browser process force killed")
                except Exception as kill_e:
                    logger.error("TurnstileSolver: failed to force kill browser: %s", kill_e)
            self._browser = None
            self._tab = None

    async def _ensure_browser(self):
        if self._browser is not None:
            return
        candidates = find_browsers()
        if not candidates:
            raise RuntimeError(
                "Bundled Playwright Chromium was not found. Reinstall "
                "SilverSpoon and keep the playwright-chromium folder beside the executable."
            )
        os.makedirs(self._profile_dir, exist_ok=True)
        last_err = None
        for bp in candidates:
            try:
                logger.info("TurnstileSolver: starting browser %s", bp)
                self._browser = await nodriver.start(
                    headless=False,
                    browser_executable_path=bp,
                    user_data_dir=self._profile_dir,
                    sandbox=False,
                    browser_args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-restore-session-state",
                        "--window-size=500,550",
                        "--window-position=50,50" if self.show_window
                        else "--window-position=-32000,-32000",
                        # Keep timers and rendering at full speed while the
                        # window is off-screen or covered by other windows.
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling",
                    ],
                )
                self._tab = await self._browser.get("about:blank")
                await self._tab.send(cdp.page.add_script_to_evaluate_on_new_document(
                    """(() => {
                        Object.defineProperty(window, 'open', {
                            configurable: true, writable: false, value: () => null,
                        });
                        const blockBlank = e => {
                            const el = e.target?.closest?.('a[target="_blank"], form[target="_blank"]');
                            if (el) { el.target = '_self'; e.preventDefault(); }
                        };
                        document.addEventListener('click', blockBlank, true);
                        document.addEventListener('auxclick', blockBlank, true);
                        const origAnchorClick = HTMLAnchorElement.prototype.click;
                        HTMLAnchorElement.prototype.click = function () {
                            if (this.target === '_blank') this.target = '_self';
                            return origAnchorClick.call(this);
                        };
                        const origFormSubmit = HTMLFormElement.prototype.submit;
                        HTMLFormElement.prototype.submit = function () {
                            if (this.target === '_blank') this.target = '_self';
                            return origFormSubmit.call(this);
                        };
                        new MutationObserver(records => {
                            for (const r of records)
                                for (const n of r.addedNodes)
                                    if (n.nodeType === 1 && n.getAttribute?.('target') === '_blank')
                                        n.setAttribute('target', '_self');
                        }).observe(document, {childList: true, subtree: true});
                    })();"""
                ))
                logger.info("TurnstileSolver: browser ready, popup creation blocked")
                return
            except Exception as e:
                last_err = e
                logger.warning("TurnstileSolver: %s failed: %s", bp, e)
                self._browser = None
        raise RuntimeError(f"No candidate browser could start. Last error: {last_err}")

    async def _navigate(self, link):
        if self._tab is None:
            logger.info("TurnstileSolver: retained tab missing; recreating")
            try:
                self._tab = await self._browser.get("about:blank")
            except Exception:
                raise RuntimeError("Failed to create new tab in browser.")

        tab = self._tab
        targets_before = len(self._browser.targets)
        logger.info(
            "TurnstileSolver: navigating tab to %s; targets_before=%s", link, targets_before,
        )
        try:
            await asyncio.wait_for(tab.get(link), timeout=15.0)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("TurnstileSolver: navigation timed out or failed (%s). Tab is likely dead.", e)
            self._tab = None
            raise RuntimeError("Tab navigation failed/hung.")

        await self._browser.update_targets()
        logger.info(
            "TurnstileSolver: navigation settled; targets=%s (delta=%+d)",
            len(self._browser.targets), len(self._browser.targets) - targets_before,
        )

        try:
            page_url = await asyncio.wait_for(
                tab.evaluate("JSON.stringify(location.href)", return_by_value=True),
                timeout=5.0
            )
            page_url = json.loads(page_url) if isinstance(page_url, str) else str(page_url)
        except Exception:
            page_url = ""

        if not (page_url.startswith("https://fuckingfast.co/") or "datanodes.to" in page_url):
            logger.warning("TurnstileSolver: unexpected URL after navigation: %s; tab might be hijacked or blank.", page_url)
            self._tab = None
            raise RuntimeError("Tab hijacked or failed to load correct URL.")
            
        logger.info("TurnstileSolver: page loaded: %s", page_url)
        return tab

    async def _ask_user(self, tab):
        """Bring the (normally off-screen) window in front of the user so they
        can complete a check that didn't pass on its own."""
        if self._revealed:
            return
        self._revealed = True
        logger.warning("TurnstileSolver: check didn't pass on its own; showing the window for the user")
        try:
            if not self.show_window:
                await tab.set_window_state(left=50, top=50, width=500, height=550, state="normal")
            await tab.bring_to_front()
        except Exception as e:
            logger.warning("TurnstileSolver: could not show the window: %s", e)
        if self._notify:
            self._notify(True)

    async def _user_done(self, tab):
        """Send the window back off-screen once the check has passed."""
        if not self._revealed:
            return
        self._revealed = False
        try:
            if not self.show_window:
                await tab.set_window_state(left=-32000, top=-32000, width=500, height=550, state="normal")
        except Exception as e:
            logger.warning("TurnstileSolver: could not hide the window: %s", e)
        if self._notify:
            self._notify(False)

    async def _resolve_fuckingfast_async(self, link, tab):
        file_id = link.split("/")[-1].split("#")[0]
        challenge_state = await tab.evaluate(
            "JSON.stringify({token:window.turnstileToken||"
            "document.querySelector('[name=\"cf-turnstile-response\"]')?.value||'',"
            "hasWidget:!!(document.querySelector('.cf-turnstile')||"
            "document.querySelector('[name=\"cf-turnstile-response\"]')||"
            "document.querySelector('iframe[src*=\"turnstile\"]'))})",
            return_by_value=True,
        )
        try:
            challenge_state = json.loads(challenge_state) if isinstance(challenge_state, str) else {}
        except (TypeError, ValueError):
            challenge_state = {}

        token = challenge_state.get("token", "")
        has_widget = bool(challenge_state.get("hasWidget"))
        logger.info("TurnstileSolver (FuckingFast): has_widget=%s token_present=%s", has_widget, bool(len(token) > 20))

        if has_widget and len(token) <= 20:
            for attempt in range(self.MAX_ATTEMPTS):
                last = attempt == self.MAX_ATTEMPTS - 1
                # The last attempt runs with the window shown, so it also gets
                # time for the user to click an interactive check.
                wait = self.TOKEN_TIMEOUT + (self.USER_SOLVE_TIME if last else 0)
                for i in range(wait):
                    if self._stop_requested:
                        raise RuntimeError("Solver stopped while waiting for token.")
                    try:
                        raw = await asyncio.wait_for(tab.evaluate(
                            "JSON.stringify(window.turnstileToken || "
                            "document.querySelector('[name=\"cf-turnstile-response\"]')?.value || null)",
                            return_by_value=True,
                        ), timeout=2.0)
                    except Exception:
                        raw = None
                        
                    if isinstance(raw, str):
                        try:
                            token = json.loads(raw)
                        except (TypeError, ValueError):
                            token = None
                        if isinstance(token, str) and len(token) > 20:
                            logger.info("TurnstileSolver (FuckingFast): token received after %ss", i + 1)
                            break
                    if i % 5 == 0:
                        logger.info("TurnstileSolver (FuckingFast): waiting for token; attempt=%s elapsed=%ss", attempt + 1, i)
                    await asyncio.sleep(1)
                    
                if isinstance(token, str) and len(token) > 20:
                    break
                    
                if not last:
                    logger.warning("TurnstileSolver (FuckingFast): no token yet; reloading with the window shown")
                    self._tab = None
                    tab = await self._navigate(link)
                    await self._ask_user(tab)
                    await asyncio.sleep(3)

            if not isinstance(token, str) or len(token) <= 20:
                waited = self.TOKEN_TIMEOUT * self.MAX_ATTEMPTS + self.USER_SOLVE_TIME
                raise RuntimeError(
                    f"Cloudflare Turnstile was not completed after {waited} seconds. "
                    "Your IP may be flagged, or the check in the browser window wasn't completed."
                )
            await self._user_done(tab)

        fetch_js = (
            "(async()=>{const r=await fetch('/f/" + file_id + "/go',"
            "{method:'POST',headers:{'HX-Request':'true','HX-Target':'',"
            "'HX-Current-URL':'" + link + "','Referer':'" + link + "'},"
            "body:new URLSearchParams({'cf-turnstile-response':window.turnstileToken||"
            "document.querySelector('[name=\"cf-turnstile-response\"]')?.value||''})});"
            "const h=r.headers.get('Hx-Redirect')||r.headers.get('hx-redirect');"
            "const t=await r.text();"
            "return JSON.stringify({status:r.status,hxRedirect:h,"
            "bodyLen:t.length,bodyHead:t.substring(0,200)});})()"
        )

        raw = await tab.evaluate(fetch_js, await_promise=True, return_by_value=True)

        if isinstance(raw, dict):
            result = raw
        elif isinstance(raw, str):
            try:
                result = json.loads(raw)
            except Exception:
                result = json.loads(raw.replace("\\'", "'").replace('\\"', '"'))
        else:
            raise RuntimeError(f"Unexpected fetch result type: {type(raw)}")

        status = result.get("status")
        direct_url = result.get("hxRedirect")
        logger.info(
            "TurnstileSolver (FuckingFast): POST /go status=%s redirect=%s body=%s",
            status, bool(direct_url), result.get("bodyHead", "")[:200],
        )
        if status != 200 or not direct_url:
            raise RuntimeError(
                f"Could not resolve direct link. POST /go returned HTTP {status}. "
                f"Response: {result.get('bodyHead', '')}"
            )
        return direct_url

    #   step 1  "Continue to Download" (#method_free, enabled after a short
    #           "Preparing File" animation) -> form POST op=download1
    #   step 2  Turnstile widget; the button only renders once it passes:
    #           "Free Download" -> countdown ("Ready in Ns") -> "Start Download"
    #           -> fetch POST op=download2 answering JSON {url}
    # Each page load wipes window state, so the fetch hook is (re)installed on
    # every tick. The hook keeps the URL and hands the page an empty reply, so
    # the solver's own browser never starts downloading the file itself.
    _DATANODES_STEP_JS = """
        JSON.stringify((() => {
            if (!window.__dn_hooked) {
                window.__dn_hooked = true;
                window.__dn_direct_url = null;
                const origFetch = window.fetch;
                window.fetch = async function(...args) {
                    const resp = await origFetch.apply(this, args);
                    try {
                        const data = await resp.clone().json();
                        if (data && data.url) {
                            window.__dn_direct_url = decodeURIComponent(data.url);
                            return new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}});
                        }
                    } catch (e) {}
                    return resp;
                };
            }
            if (window.__dn_direct_url) {
                return {url: window.__dn_direct_url};
            }
            const links = Array.from(document.querySelectorAll('a')).map(a => a.href).filter(h => h && (h.includes('dlproxy') || h.includes('.rar') || (h.includes(':8080') && !h.includes('datanodes.to/app'))));
            if (links.length) {
                return {url: links[0]};
            }
            const hasCaptcha = !!document.querySelector('.cf-turnstile, iframe[src*="turnstile"]');
            const btns = Array.from(document.querySelectorAll('button, input[type="submit"]'));
            for (const b of btns) {
                // First line only: the buttons carry a subtitle ("Standard speed").
                const txt = (b.innerText || b.value || '').trim().split('\\n')[0].trim();
                if (!/^(Continue to Download|Free Download|Start Download)/i.test(txt)) continue;
                // Disabled while "Preparing File" runs; give a click 5 s to take effect.
                if (b.disabled || !b.offsetParent || Date.now() - (+b.dataset.dnClicked || 0) < 5000) {
                    return {waiting: txt, hasCaptcha};
                }
                b.dataset.dnClicked = Date.now();
                b.scrollIntoView({behavior: 'instant', block: 'center'});
                b.click();
                return {clicked: txt, hasCaptcha};
            }
            return {hasCaptcha};
        })())
    """

    async def _resolve_datanodes_async(self, link, tab):
        direct_url = None

        def on_network_response(event: cdp.network.ResponseReceived):
            nonlocal direct_url
            url = event.response.url
            if any(keyword in url for keyword in ['dlproxy', '/d/', '.rar']) and 'datanodes.to' not in url:
                direct_url = url
                logger.info("TurnstileSolver (DataNodes): captured direct URL via CDP network: %s", direct_url[:60])

        tab.add_handler(cdp.network.ResponseReceived, on_network_response)
        await tab.send(cdp.network.enable())

        logger.info("TurnstileSolver (DataNodes): working through download steps...")
        # Turnstile gets reveal_after seconds to pass on its own, then the window
        # is shown and the user gets USER_SOLVE_TIME more to complete it.
        reveal_after = max(10, self.TOKEN_TIMEOUT)
        captcha_timeout = reveal_after + self.USER_SOLVE_TIME
        # Step 1 animation (~10 s) + Turnstile + countdown (~10 s) + POST;
        # extended below if the user is asked to solve the check.
        deadline = asyncio.get_running_loop().time() + max(30, self.TOKEN_TIMEOUT + 15) + 45
        captcha_since = None
        state = {}
        while not direct_url:
            if self._stop_requested:
                raise RuntimeError("Solver stopped while waiting for DataNodes download link.")
            now = asyncio.get_running_loop().time()
            if now > deadline:
                break
            await asyncio.sleep(1)
            try:
                state = json.loads(await tab.evaluate(self._DATANODES_STEP_JS)) or {}
            except Exception:
                # Mid-navigation (step 1 submits a form); try again next tick.
                continue

            if state.get("url"):
                direct_url = state["url"]
                logger.info("TurnstileSolver (DataNodes): captured direct URL from page state: %s", direct_url[:60])
                break
            if state.get("clicked"):
                logger.info("TurnstileSolver (DataNodes): clicked '%s'", state["clicked"])

            # Turnstile showing with no download button yet means it hasn't passed.
            if state.get("hasCaptcha") and not (state.get("clicked") or state.get("waiting")):
                captcha_since = captcha_since or now
                if now - captcha_since > reveal_after and not self._revealed:
                    await self._ask_user(tab)
                    deadline += self.USER_SOLVE_TIME
                if now - captcha_since > captcha_timeout:
                    raise RuntimeError(
                        f"DataNodes Turnstile was not completed after {captcha_timeout} seconds. "
                        "Your IP may be flagged, or the check in the browser window wasn't completed."
                    )
            else:
                captcha_since = None
                await self._user_done(tab)  # passed (no-op if it was never shown)

        if not direct_url:
            raise RuntimeError(
                "DataNodes did not produce a direct download link "
                f"(last step seen: {state.get('clicked') or state.get('waiting') or 'no download button'}). "
                "The page layout may have changed."
            )

        return direct_url

    async def _resolve_direct_link_async(self, link, notify=None):
        async with self._browser_lock:
            self._notify, self._revealed = notify, False
            await self._ensure_browser()
            try:
                logger.info("TurnstileSolver: resolving %s", link)
                tab = await self._navigate(link)
            except Exception as e:
                logger.error("TurnstileSolver: initial navigation failed: %s", e)
                await self._close_browser()
                await self._ensure_browser()
                tab = await self._navigate(link)

            await asyncio.sleep(2)

            try:
                if "datanodes.to" in link.lower():
                    direct_url = await self._resolve_datanodes_async(link, tab)
                else:
                    direct_url = await self._resolve_fuckingfast_async(link, tab)
            finally:
                # Never leave the window on screen (a failure closes the browser).
                if self._revealed and self._tab is not None:
                    await self._user_done(self._tab)
                self._notify, self._revealed = None, False

            ua_raw = await tab.evaluate("JSON.stringify(navigator.userAgent)", return_by_value=True)
            user_agent = json.loads(ua_raw) if isinstance(ua_raw, str) else str(ua_raw)

            raw_cookies = await self._browser.cookies.get_all()
            cookies = {c.name: c.value for c in raw_cookies}

            self._user_agent = user_agent
            self._cookies = cookies
            logger.info("TurnstileSolver: resolved -> %s", direct_url[:60])
            
            # Navigating back to about:blank cleans up the solver window visually
            # and prevents the page from continuing to execute scripts or use RAM
            # while waiting for the next link.
            try:
                await self._tab.get("about:blank")
            except Exception as e:
                logger.warning("TurnstileSolver: could not clear tab after solve: %s", e)
                
            return {"direct_url": direct_url, "cookies": cookies, "user_agent": user_agent}

    def get_direct_link(self, link, notify=None):
        """notify(True) is called when the window is shown because the user
        must complete the check, notify(False) once it's hidden again."""
        return self._submit(self._resolve_with_cleanup(link, notify))

    async def _resolve_with_cleanup(self, link, notify=None):
        try:
            return await self._resolve_direct_link_async(link, notify)
        except Exception:
            # Any failed resolution tears the browser down so a broken instance
            # is never reused — reuse was causing repeated browser re-spawns that
            # never reached the download page.
            async with self._browser_lock:
                await self._close_browser()
            raise

    @property
    def user_agent(self):
        return self._user_agent

    @property
    def cookies(self):
        return self._cookies or {}

    def stop(self):
        self._stop_requested = True
        if self._browser is not None and self._loop is not None:
            try:
                self._submit(self._stop_browser_async())
            except Exception:
                pass
        self._browser = None
        if self._loop is not None and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        self._loop = None

    async def _stop_browser_async(self):
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:
                pass
