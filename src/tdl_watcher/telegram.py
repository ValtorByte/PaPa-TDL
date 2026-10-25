"""Telegram Web K browser login and group media downloads."""

import asyncio
import getpass
import math
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path

from playwright.async_api import Error as BrowserError, async_playwright

from .config import Settings
from .ui import ProgressBar, message


WEB_URL = "https://web.telegram.org/k/"
PHONE_INPUT = '#auth-pages .input-field-phone .input-field-input:visible'
CODE_INPUT = '#auth-pages input[autocomplete="one-time-code"]:visible, #auth-pages input[name="sms-word"]:visible'
PASSWORD_INPUT = '#auth-pages input[type="password"]:visible'
MIN_FREE_BYTES = 512 * 1024 * 1024


class TelegramError(RuntimeError):
    """Safe error without login secrets or message contents."""


def normalize_phone(value: str) -> str | None:
    """Accept E.164, 00-prefixed international, or an Iranian mobile number."""
    value = re.sub(r"[\s()\-]", "", value.strip())
    if re.fullmatch(r"09[0-9]{9}", value):
        value = "+98" + value[1:]
    elif re.fullmatch(r"989[0-9]{9}", value):
        value = "+" + value
    elif re.fullmatch(r"\+9809[0-9]{9}", value):
        value = "+98" + value[4:]
    elif value.startswith("00"):
        value = "+" + value[2:]
    return value if re.fullmatch(r"\+[1-9][0-9]{6,14}", value) else None


def safe_filename(value: str, fallback: str) -> str:
    name = unicodedata.normalize("NFKC", str(value or fallback))
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or fallback
    suffix = Path(name).suffix
    suffix = suffix if len(suffix.encode("utf-8")) <= 20 else ""
    stem = name[:-len(suffix)] if suffix else name
    while len((stem + suffix).encode("utf-8")) > 180:
        stem = stem[:-1]
    return (stem or "file") + suffix


def estimated_bytes(label: str) -> int:
    scale = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    sizes = [float(number.replace(",", "")) * scale[unit.upper()]
             for number, unit in re.findall(r"([0-9]+(?:[.,][0-9]+)?)\s*(TB|GB|MB|KB|B)\b", label, re.I)]
    return math.ceil(max(sizes)) if sizes else 0


class TelegramBrowser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.playwright = None
        self.context = None
        self.page = None

    async def start(self) -> None:
        await self.stop()
        self.playwright = await async_playwright().start()
        launch_options = {}
        executable = os.environ.get("TDL_CHROMIUM_EXECUTABLE", "").strip()
        if executable:
            executable_path = Path(executable)
            if not executable_path.is_file():
                raise TelegramError(
                    f"Configured browser does not exist: {executable_path}. Reinstall PaPa-TDL."
                )
            launch_options["executable_path"] = str(executable_path)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.settings.data_dir / "browser-profile"), headless=True,
            accept_downloads=True,
            downloads_path=str(self.settings.data_dir / "browser-downloads"),
            viewport={"width": 1100, "height": 800}, locale="en-US",
            ignore_https_errors=False, service_workers="allow",
            args=["--disable-dev-shm-usage"], timeout=60000,
            **launch_options,
        )
        self.context.set_default_timeout(20000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(WEB_URL, wait_until="domcontentloaded", timeout=90000)
        await self.page.locator("#page-chats:visible, #auth-pages:visible").first.wait_for(
            state="visible", timeout=60000)

    async def stop(self) -> None:
        context, runner = self.context, self.playwright
        self.context = self.playwright = self.page = None
        try:
            if context:
                try:
                    await context.close()
                except BrowserError:
                    pass
        finally:
            if runner:
                try:
                    await runner.stop()
                except BrowserError:
                    pass

    async def logged_in(self) -> bool:
        try:
            return (self.page is not None and not self.page.is_closed()
                    and await self.page.locator("#page-chats").is_visible())
        except BrowserError:
            return False

    async def _wait_step(self, *, code=False, password=False, timeout=120) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.logged_in():
                return "success"
            if password and await self.page.locator(PASSWORD_INPUT).count() == 1:
                return "password"
            if code and await self.page.locator(CODE_INPUT).count() == 1:
                return "code"
            alerts = await self.page.locator('#auth-pages [role="alert"]:visible').all_text_contents()
            messages = " ".join(alerts).upper()
            if "FLOOD" in messages or "TOO MANY" in messages:
                raise TelegramError("Telegram rate-limited login; wait before trying again.")
            if "EXPIRED" in messages:
                raise TelegramError("The Telegram login code expired; start login again.")
            if "INVALID" in messages or "INCORRECT" in messages:
                return "invalid"
            if await self.page.locator('#auth-pages input[type="email"]:visible').count():
                raise TelegramError("Telegram requires additional email verification in its official client.")
            await asyncio.sleep(0.5)
        raise TelegramError("Telegram did not finish this login step within two minutes.")

    async def login(self) -> None:
        if await self.logged_in():
            message("Existing private Telegram Web session reused.", "success")
            return
        page = self.page
        phone_box = page.locator(PHONE_INPUT)
        if await phone_box.count() == 0:
            button = page.get_by_role("button", name=re.compile(r"log in (?:by|with) phone number", re.I))
            await button.wait_for(state="visible", timeout=90000)
            await button.click()
        await phone_box.wait_for(state="visible", timeout=90000)
        message("Your phone number is visible while typing; codes and passwords stay hidden.", "info")
        for _ in range(3):
            phone = normalize_phone(input("Phone (+country code, e.g. +98912...; Iran 0912... works): "))
            if phone:
                break
            message("Use +country code, or an Iranian mobile number starting with 09.", "warning")
        else:
            raise TelegramError("Invalid phone number format after three attempts.")
        await phone_box.fill(phone)
        phone = None
        next_button = page.locator('#auth-pages .input-wrapper button.btn-color-primary:visible')
        if await next_button.count() != 1:
            raise TelegramError("Telegram Web's phone-login button could not be identified.")
        await next_button.click()
        state = await self._wait_step(code=True, password=True)
        if state == "code":
            message("Enter the code sent by Telegram.", "info")
            for _ in range(3):
                box = page.locator(CODE_INPUT)
                if await box.count() != 1:
                    raise TelegramError("Telegram Web's code field could not be identified.")
                text_code = await box.get_attribute("name") == "sms-word"
                secret = getpass.getpass("Telegram login code (hidden): ").strip()
                if not secret or (not text_code and not re.fullmatch(r"[0-9]{3,32}", secret)):
                    message("Enter the code exactly as received.", "warning")
                    continue
                max_length = await box.get_attribute("maxlength")
                if not max_length and not text_code:
                    digits = await box.locator('..').locator(':scope > div[class*="_digit_"]').count()
                    max_length = str(digits) if digits else None
                if max_length and not text_code and len(secret) != int(max_length):
                    message("The code length is incorrect.", "warning")
                    continue
                await box.fill(secret)
                secret = None
                if text_code:
                    await box.press("Enter")
                state = await self._wait_step(password=True)
                if state == "invalid":
                    message("Telegram rejected the code; try again.", "error")
                    continue
                break
            else:
                raise TelegramError("Telegram login code was not accepted.")
        if state == "password":
            for _ in range(3):
                box = page.locator(PASSWORD_INPUT)
                if await box.count() != 1:
                    raise TelegramError("Telegram Web's two-step password field is unavailable.")
                secret = getpass.getpass("Telegram two-step password (hidden): ")
                await box.fill(secret)
                secret = None
                await box.press("Enter")
                state = await self._wait_step()
                if state == "invalid":
                    message("Telegram rejected the password; try again.", "error")
                    continue
                break
            else:
                raise TelegramError("Telegram two-step verification failed.")
        if state != "success" or not await self.logged_in():
            raise TelegramError("Telegram login was not completed. Check the official client.")
        message("Telegram sign-in completed. The private session is saved on this machine.", "success")

    async def find_group(self) -> str:
        if not await self.logged_in():
            raise TelegramError("Telegram Web is signed out; run the interactive login command.")
        name = self.settings.group_name
        search = self.page.locator("#column-left .input-search-input:visible")
        await search.wait_for(state="visible", timeout=30000)
        await search.fill(name)
        deadline, matches = time.monotonic() + 45, {}
        while time.monotonic() < deadline:
            matches = {}
            for row in await self.page.locator("#column-left .chatlist-chat[data-peer-id]").all():
                texts = await row.locator(".dialog-title, .row-title").all_text_contents()
                if any(text.strip() == name for text in texts):
                    peer = await row.get_attribute("data-peer-id")
                    if peer and re.fullmatch(r"-[1-9][0-9]{0,16}", peer):
                        matches[peer] = row
            if len(matches) == 1:
                break
            await asyncio.sleep(0.5)
        if len(matches) != 1:
            raise TelegramError("Exactly one accessible Telegram group with the configured name is required.")
        peer, row = next(iter(matches.items()))
        await row.click()
        header = self.page.locator("#column-center .topbar:visible .user-title:visible")
        await header.wait_for(state="visible", timeout=20000)
        if (await header.inner_text()).strip() != name:
            raise TelegramError("The selected Telegram group title changed; monitoring stopped.")
        await self.page.locator("#column-center .bubbles-scrollable:visible").wait_for(timeout=20000)
        return peer

    async def visible_media(self, peer: str, *, at_current_position: bool = False) -> list[dict]:
        if not at_current_position:
            await self.page.evaluate("""() => {
              const scroller = document.querySelector('#column-center .bubbles-scrollable');
              if (scroller) scroller.scrollTop = scroller.scrollHeight;
            }""")
            await asyncio.sleep(0.5)
        return await self.page.evaluate("""peer => {
          const root = document.querySelector('#column-center');
          if (!root) return [];
          const result = [];
          const media = '.document, .audio, .media-photo, .media-video, .media-sticker-wrapper, .media-round';
          for (const bubble of root.querySelectorAll('.bubble[data-peer-id][data-mid]')) {
            if (bubble.dataset.peerId !== peer || bubble.classList.contains('service')) continue;
            const children = bubble.querySelectorAll('.grouped-item[data-mid], .document-container[data-mid]');
            const items = children.length ? [...children] : [bubble];
            for (const item of items) {
              if (!item.querySelector(media)) continue;
              const mid = item.dataset.mid;
              if (!/^[1-9][0-9]{0,19}$/.test(mid || '')) continue;
              const kind = item.classList.contains('grouped-item') ? 'grouped-item' :
                           item.classList.contains('document-container') ? 'document-container' : 'bubble';
              const document = item.querySelector('.document');
              const named = item.querySelector('.document-name [title], .audio-title [title], [data-file-name]');
              const label = named?.getAttribute('title') || named?.getAttribute('data-file-name') ||
                            item.querySelector('.document-name, .audio-title')?.textContent ||
                            item.innerText?.slice(0, 100) || 'media';
              const docId = document?.getAttribute('data-doc-id') || null;
              const size = item.querySelector('.document-size, .document-description')?.textContent || '';
              result.push({peer, mid, kind, label, size, doc_id: docId, album: bubble.dataset.mid});
            }
          }
          return result;
        }""", peer)

    async def _events_on_screen(self, peer: str) -> list[dict]:
        media = await self.visible_media(peer, at_current_position=True)
        commands = await self.page.evaluate(r"""peer => {
          const root = document.querySelector('#column-center');
          if (!root) return [];
          const result = [];
          for (const bubble of root.querySelectorAll('.bubble[data-peer-id][data-mid]')) {
            if (bubble.dataset.peerId !== peer || bubble.classList.contains('service')) continue;
            const outgoing = bubble.classList.contains('is-out') ||
              bubble.classList.contains('is-outgoing') || bubble.classList.contains('own') ||
              !!bubble.querySelector('.is-out, .is-outgoing, .own');
            if (!outgoing) continue;
            const mid = bubble.dataset.mid;
            if (!/^[1-9][0-9]{0,19}$/.test(mid || '')) continue;
            const body = bubble.querySelector('.translatable-message, .message, .bubble-content');
            const text = (body?.innerText || '').trim();
            if (/^\/(extract|done)(?:@\w+)?\s*$/i.test(text)) {
              result.push({type: 'command', peer, mid, text});
            }
          }
          return result;
        }""", peer)
        return [{**item, "type": "media"} for item in media] + commands

    async def visible_events(self, peer: str, *, since_mid: str = "0") -> list[dict]:
        """Read recent chat history, including items Telegram has virtualized off screen."""
        await self.page.evaluate("""() => {
          const scroller = document.querySelector('#column-center .bubbles-scrollable');
          if (scroller) scroller.scrollTop = scroller.scrollHeight;
        }""")
        await asyncio.sleep(0.25)
        found: dict[tuple[str, str], dict] = {}
        unchanged = 0
        try:
            for _ in range(80):
                for item in await self._events_on_screen(peer):
                    found[(item["type"], item["mid"])] = item
                if since_mid != "0" and any(int(item["mid"]) <= int(since_mid)
                                            for item in found.values()):
                    break
                before = await self.page.evaluate("""() => {
                  const s = document.querySelector('#column-center .bubbles-scrollable');
                  return s ? [s.scrollTop, s.scrollHeight] : [0, 0];
                }""")
                await self.page.evaluate("""() => {
                  const s = document.querySelector('#column-center .bubbles-scrollable');
                  if (s) s.scrollTop = Math.max(0, s.scrollTop - Math.max(400, s.clientHeight * 0.8));
                }""")
                await asyncio.sleep(0.2)
                after = await self.page.evaluate("""() => {
                  const s = document.querySelector('#column-center .bubbles-scrollable');
                  return s ? [s.scrollTop, s.scrollHeight] : [0, 0];
                }""")
                unchanged = unchanged + 1 if after == before else 0
                if unchanged and after[0] == 0:
                    # Telegram Web virtualizes history and may need more than one
                    # animation frame before it replaces the rendered page.
                    await self.page.evaluate("""() => {
                      const s = document.querySelector('#column-center .bubbles-scrollable');
                      if (s) s.dispatchEvent(new Event('scroll', {bubbles: true}));
                    }""")
                    await asyncio.sleep(1.25)
                    latest = await self.page.evaluate("""() => {
                      const s = document.querySelector('#column-center .bubbles-scrollable');
                      return s ? [s.scrollTop, s.scrollHeight] : [0, 0];
                    }""")
                    if latest != after:
                        unchanged = 0
                if unchanged >= 3:
                    break
        finally:
            await self.page.evaluate("""() => {
              const s = document.querySelector('#column-center .bubbles-scrollable');
              if (s) s.scrollTop = s.scrollHeight;
            }""")
        return sorted(found.values(), key=lambda item: int(item["mid"]))

    async def _reveal_media(self, peer: str, mid: str, kind: str) -> None:
        """Scroll older chat pages until the selected message is rendered again."""
        await self.page.evaluate("""() => {
          const s = document.querySelector('#column-center .bubbles-scrollable');
          if (s) s.scrollTop = s.scrollHeight;
        }""")
        selector = f'#column-center .bubble[data-peer-id="{peer}"]'
        selector += f'[data-mid="{mid}"]' if kind == "bubble" else f' .{kind}[data-mid="{mid}"]'
        for _ in range(80):
            if await self.page.locator(selector).count() == 1:
                return
            before = await self.page.evaluate("""() => {
              const s = document.querySelector('#column-center .bubbles-scrollable');
              return s ? [s.scrollTop, s.scrollHeight] : [0, 0];
            }""")
            await self.page.evaluate("""() => {
              const s = document.querySelector('#column-center .bubbles-scrollable');
              if (s) s.scrollTop = Math.max(0, s.scrollTop - Math.max(400, s.clientHeight * 0.8));
            }""")
            await asyncio.sleep(0.2)
            after = await self.page.evaluate("""() => {
              const s = document.querySelector('#column-center .bubbles-scrollable');
              return s ? [s.scrollTop, s.scrollHeight] : [0, 0];
            }""")
            if after == before and before[0] == 0:
                await asyncio.sleep(0.6)
                if await self.page.locator(selector).count() == 1:
                    return
                again = await self.page.evaluate("""() => {
                  const s = document.querySelector('#column-center .bubbles-scrollable');
                  return s ? [s.scrollTop, s.scrollHeight] : [0, 0];
                }""")
                if again == after:
                    break
        raise TelegramError("The selected archive part could not be found in Telegram history.")

    async def send_text(self, text: str) -> None:
        """Send a short status message to the currently open group."""
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise TelegramError("Invalid Telegram status message.")
        composer = self.page.locator(
            '#column-center .input-message-input:visible, #column-center [contenteditable="true"]:visible'
        ).last
        await composer.wait_for(state="visible", timeout=10000)
        await composer.fill(text)
        await composer.press("Enter")

    async def download(self, spec: dict, destination: Path | None = None) -> Path:
        peer, mid, kind = (spec[k] for k in ("peer", "mid", "kind"))
        if not re.fullmatch(r"-[1-9][0-9]{0,16}", peer) or not re.fullmatch(r"[1-9][0-9]{0,19}", mid):
            raise TelegramError("Invalid Telegram message identity.")
        if kind not in {"bubble", "grouped-item", "document-container"}:
            raise TelegramError("Invalid Telegram media type.")
        root = f'#column-center .bubble[data-peer-id="{peer}"]'
        selector = root + f'[data-mid="{mid}"]' if kind == "bubble" else root + f' .{kind}[data-mid="{mid}"]'
        await self._reveal_media(peer, mid, kind)
        target = self.page.locator(selector)
        if await target.count() != 1:
            raise TelegramError("The Telegram media is no longer visible; it was not downloaded.")
        media = target.locator(".document, .audio, .media-photo, .media-video, .media-sticker-wrapper, .media-round")
        if await media.count() == 0:
            raise TelegramError("The selected Telegram message has no downloadable media.")
        labels = await target.locator(".document-description, .document-size").all_text_contents()
        estimate = estimated_bytes(" ".join(labels))
        if self.settings.max_download_bytes and estimate > self.settings.max_download_bytes:
            raise TelegramError("Telegram media exceeds TDL_MAX_DOWNLOAD_BYTES.")
        if shutil.disk_usage(self.settings.data_dir).free < MIN_FREE_BYTES + int(estimate * 2.1):
            raise TelegramError("Insufficient local disk space for a browser download.")
        owns_destination = destination is None
        destination = destination or self.settings.pending_dir / f"{peer[1:]}_{mid}"
        if owns_destination and destination.exists() and list(destination.iterdir()):
            raise TelegramError("A pending local copy already exists for this message; retry its upload first.")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = None
        created_path = False
        browser_path = None
        progress = ProgressBar("Telegram download", estimate or None)
        try:
            download_waiter = asyncio.create_task(self.page.wait_for_event("download", timeout=30 * 60 * 1000))
            await media.first.click(button="right")
            items = self.page.locator(".btn-menu.active .btn-menu-item:visible")
            text = self.page.locator(".btn-menu-item-text").filter(has_text=re.compile(r"^\s*Download\s*$", re.I))
            choice = items.filter(has=text)
            await choice.wait_for(state="visible", timeout=10000)
            if await choice.count() != 1 or "is-disabled" in (await choice.get_attribute("class") or ""):
                raise TelegramError("Telegram Web does not offer an enabled Download action for this file.")
            await choice.click()
            downloaded = await self._monitor(download_waiter, progress)
            path_waiter = asyncio.create_task(downloaded.path())
            browser_path = await self._monitor(path_waiter, progress)
            if await downloaded.failure() or not browser_path:
                raise TelegramError("Telegram Web did not complete the file download.")
            browser_path = Path(browser_path)
            if not browser_path.is_file() or browser_path.stat().st_size <= 0:
                raise TelegramError("Telegram Web produced an empty or missing file.")
            # The UI size is rounded and sometimes uses a different unit.
            # Playwright's completed download event and failure() are the
            # authoritative signals; retain the exact observed byte count.
            if self.settings.max_download_bytes and browser_path.stat().st_size > self.settings.max_download_bytes:
                raise TelegramError("Telegram media exceeds TDL_MAX_DOWNLOAD_BYTES.")
            filename = safe_filename(downloaded.suggested_filename, f"telegram_{mid}.bin")
            if filename in {"upload.json", "upload.json.tmp", "source.json", "source.tmp", "payload.part"}:
                filename = "file_" + filename
            path = destination / filename
            if path.exists():
                # Re-forwarding the same multipart archive is common.  Treat a
                # byte-identical volume as the same input, without touching the
                # already staged copy.  A conflicting volume remains an error.
                if path.stat().st_size == browser_path.stat().st_size:
                    import hashlib

                    def digest(candidate):
                        value = hashlib.sha256()
                        with candidate.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                                value.update(chunk)
                        return value.digest()

                    if await asyncio.to_thread(digest, path) == await asyncio.to_thread(digest, browser_path):
                        browser_path.unlink(missing_ok=True)
                        progress.finish(f"{path.name} already staged")
                        return path
                raise TelegramError(
                    "A different file with the same archive-part name is already staged; "
                    "rename or remove the conflicting Telegram upload."
                )
            shutil.move(str(browser_path), str(path))
            created_path = True
            progress.update(path.stat().st_size, total=path.stat().st_size)
            progress.finish(f"{path.name} ready")
            print(f"Downloaded: {path.name} ({path.stat().st_size} bytes).", flush=True)
            return path
        except BaseException:
            progress.finish("failed; retry is safe", success=False)
            if owns_destination:
                shutil.rmtree(destination, ignore_errors=True)
            elif created_path and path is not None:
                path.unlink(missing_ok=True)
            if browser_path is not None and browser_path != path:
                browser_path.unlink(missing_ok=True)
            raise
        finally:
            if "download_waiter" in locals() and not download_waiter.done():
                download_waiter.cancel()
                await asyncio.gather(download_waiter, return_exceptions=True)
            if "path_waiter" in locals() and not path_waiter.done():
                path_waiter.cancel()
                await asyncio.gather(path_waiter, return_exceptions=True)

    async def _monitor(self, future: asyncio.Task, progress: ProgressBar):
        while not future.done():
            if shutil.disk_usage(self.settings.data_dir).free < MIN_FREE_BYTES:
                raise TelegramError("Local disk space ran low during the Telegram download.")
            total = 0
            for path in (self.settings.data_dir / "browser-downloads").rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    pass
            if self.settings.max_download_bytes and total > self.settings.max_download_bytes:
                raise TelegramError("Telegram download exceeded TDL_MAX_DOWNLOAD_BYTES.")
            progress.update(total)
            await asyncio.wait([future], timeout=5)
        result = await future
        return result
