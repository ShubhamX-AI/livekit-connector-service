import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ..browser.payload import build_initial_script
from ..browser.websocket import BrowserWebSocketServer
from ..config import ConnectorConfig
from ..events import READY_EVENT, WAITING_EVENT
from .humanized_input import HumanizedInput

logger = logging.getLogger(__name__)
JOIN_RETRY_DELAY_SECONDS = 5
ADMISSION_POLL_SECONDS = 2
BLOCKED_TEXTS = ("You can't join this video call", "There is a problem connecting to this video call")
DENIED_TEXTS = (
    "Someone in the call denied your request to join",
    "Someone on the call denied your request to join",
    "Someone in the call has denied your request to join",
    "Someone on the call has denied your request to join",
    "No one responded to your request to join the call",
    "No one has responded to your request to join the call",
    "You left the meeting",
)
WAITING_ROOM_TEXT = "Asking to be let in"
LEAVE_CALL_SELECTOR = 'button[aria-label="Leave call"]'
JOIN_BUTTON_XPATH = '//button[.//span[text()="Ask to join" or text()="Ask to join anyway" or text()="Join now" or text()="Join the call now" or text()="Join anyway" or text()="Join here too"]]'


class GoogleBlockingJoinError(RuntimeError):
    """Google rejected the join attempt with its generic block screen."""


class JoinRequestDeniedError(RuntimeError):
    """Someone in the call denied the join request, or nobody answered it."""


class WaitingRoomTimeoutError(RuntimeError):
    """Nobody admitted the bot before the waiting-room timeout expired."""


class GoogleMeetChromeSession:
    def __init__(
        self,
        config: ConnectorConfig,
        livekit_sync,
        on_status: Callable[[str, str | None], None] | None = None,
    ):
        self.config = config
        self.livekit_sync = livekit_sync
        self.on_status = on_status
        self.display = None
        self.driver = None
        self.humanized_input = None
        self.stop_requested = threading.Event()
        self.websocket_server = BrowserWebSocketServer(
            host=config.websocket_host,
            port=config.websocket_port,
            livekit_sync=livekit_sync,
            upstream_livekit_url=config.livekit_url,
            on_status=on_status,
            alone_in_meeting_timeout_seconds=config.alone_in_meeting_timeout_seconds,
        )

    @property
    def humanized(self):
        return self.config.ui_interaction_mode == "humanized"

    def start(self):
        self._start_display()
        self.websocket_server.start()
        self._join_meeting()

    def _start_display(self):
        if os.environ.get("DISPLAY"):
            return
        self.display = Display(visible=0, size=(1930, 1090), use_xauth=True)
        self.display.start()

    def _start_driver(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--use-fake-device-for-media-stream")
        options.add_argument("--use-fake-ui-for-media-stream")
        options.add_argument(f"--window-size={self.config.video_frame_width},{self.config.video_frame_height}")
        options.add_argument("--start-fullscreen")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-application-cache")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("prefs", {"credentials_enable_service": False, "profile.password_manager_enabled": False})
        if self.config.headless:
            options.add_argument("--headless=new")
        if os.environ.get("ENABLE_CHROME_SANDBOX", "false").lower() != "true":
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-setuid-sandbox")

        self.driver = webdriver.Chrome(options=options, service=Service(executable_path=self.config.chrome_driver_path))
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": self._browser_script()})
        self.humanized_input = HumanizedInput(self.driver, video_frame_size=(self.config.video_frame_width, self.config.video_frame_height)) if self.humanized else None

    def _quit_driver(self):
        self.humanized_input = None
        if not self.driver:
            return
        try:
            self.driver.quit()
        except Exception:
            logger.exception("Failed to close Chrome")
        self.driver = None

    def _browser_script(self):
        initial_data = {
            "websocketPort": self.config.websocket_port,
            "videoFrameWidth": self.config.video_frame_width,
            "videoFrameHeight": self.config.video_frame_height,
            "botName": self.config.bot_display_name,
            "addClickRipple": False,
            "recordingView": "speaker_view",
            # The assistant listens to one linked participant, so the connector always publishes
            # Google's mixed audio rather than one track per speaker.
            # Pins the mixing AudioContext's rate so the float32 frames the page sends match the
            # rate we declare to LiveKit. Left to the browser's default, a host that negotiated
            # 44.1 kHz would produce audio played back ~9% fast.
            "audioSampleRate": self.config.sample_rate,
            "sendMixedAudio": True,
            "perParticipantRealtimeVideoConfiguration": {
                "webcam_configuration": {"enabled": False},
                "screenshare_configuration": {"enabled": False},
            },
            "roomSyncSourceParticipantConfiguration": self.livekit_sync.source_browser_config(),
            "sendPerParticipantVideo": False,
            "collectCaptions": False,
            "recordParticipantSpeechStartStopEvents": False,
        }
        return build_initial_script(initial_data)

    def _join_meeting(self):
        # Google sometimes rejects a join with "You can't join this video call". That block is
        # often tied to the browser session, so every attempt gets a fresh Chrome rather than
        # reloading the URL in the session Google already rejected.
        attempts = self.config.join_attempts
        last_error = None
        for attempt in range(1, attempts + 1):
            logger.info("Opening Google Meet (attempt %s/%s): %s", attempt, attempts, self.config.meeting_url)
            try:
                self._start_driver()
                self._attempt_join()
                logger.info("Google Meet join succeeded")
                return
            except (GoogleBlockingJoinError, JoinRequestDeniedError, WaitingRoomTimeoutError) as error:
                last_error = error
                logger.warning("Join attempt %s failed: %s: %s", attempt, type(error).__name__, error)
                self._save_artifacts(f"attempt-{attempt}-{type(error).__name__}")
                self._quit_driver()
                if isinstance(error, GoogleBlockingJoinError) and attempt < attempts:
                    time.sleep(JOIN_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise
            except Exception as error:
                last_error = error
                logger.exception("Join attempt %s raised an unexpected error", attempt)
                self._save_artifacts(f"attempt-{attempt}-{type(error).__name__}")
                self._quit_driver()
                if attempt < attempts:
                    time.sleep(JOIN_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise
        raise RuntimeError(f"Google blocked every join attempt ({attempts}). Joining as a signed-in Google account usually avoids this.") from last_error

    def _attempt_join(self):
        if self.humanized_input:
            self.humanized_input.position_mouse()
        self.driver.get(self.config.meeting_url)
        self.driver.execute_cdp_cmd("Browser.grantPermissions", {"origin": self.config.meeting_url, "permissions": ["audioCapture", "videoCapture"]})
        self._reject_invalid_meeting()
        self._raise_if_blocked()
        self._fill_name()
        self._turn_off_media()
        join_button = WebDriverWait(self.driver, 60).until(EC.presence_of_element_located((By.XPATH, JOIN_BUTTON_XPATH)))
        logger.info("Clicking the join button")
        if self.humanized_input:
            self.humanized_input.click_element(join_button)
        else:
            join_button.click()
        self._wait_until_admitted()
        self.driver.execute_script("window.ws?.enableMediaSending();")
        logger.info("Media sending enabled")
        self._emit_status(READY_EVENT, None)

    def _wait_until_admitted(self):
        """Block until the bot is actually in the call.

        A block screen shows up within a few seconds of the click. Otherwise the bot is
        either already in the call or sitting in the waiting room until someone admits it.
        Media sending must not start before this returns, or the bot publishes audio while
        it is still in the waiting room.
        """
        deadline = time.monotonic() + self.config.waiting_room_timeout_seconds
        logged_waiting = False
        while time.monotonic() < deadline:
            if self.stop_requested.is_set():
                raise RuntimeError("Connector shutdown requested")
            self._raise_if_blocked()
            self._raise_if_denied()
            if self._is_in_call():
                logger.info("Bot is in the call")
                return
            if not logged_waiting and self._page_contains(WAITING_ROOM_TEXT):
                logger.info("Waiting to be admitted (timeout %ss)", self.config.waiting_room_timeout_seconds)
                logged_waiting = True
                self._emit_status(WAITING_EVENT, WAITING_ROOM_TEXT)
            time.sleep(ADMISSION_POLL_SECONDS)
        raise WaitingRoomTimeoutError(f"Not admitted within {self.config.waiting_room_timeout_seconds}s")

    def _is_in_call(self):
        return bool(self.driver.find_elements(By.CSS_SELECTOR, LEAVE_CALL_SELECTOR))

    def _page_contains(self, text):
        return bool(self._visible_elements_containing((text,)))

    def _emit_status(self, event: str, detail: str | None) -> None:
        if self.on_status:
            self.on_status(event, detail)

    def _visible_elements_containing(self, texts):
        xpath = "//*[" + " or ".join(f'contains(text(), "{text}")' for text in texts) + "]"
        return [element for element in self.driver.find_elements(By.XPATH, xpath) if element.is_displayed()]

    def _raise_if_blocked(self):
        for element in self._visible_elements_containing(BLOCKED_TEXTS):
            raise GoogleBlockingJoinError(element.text.strip().splitlines()[0])

    def _raise_if_denied(self):
        for element in self._visible_elements_containing(DENIED_TEXTS):
            raise JoinRequestDeniedError(element.text.strip().splitlines()[0])

    def _reject_invalid_meeting(self):
        invalid_texts = ("Check your meeting code", "Invalid video call name", "Your meeting code has expired")
        body = WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        if any(text in body.text for text in invalid_texts):
            raise RuntimeError("Google Meet URL is invalid or expired")

    def _fill_name(self):
        try:
            name_input = WebDriverWait(self.driver, 30).until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="text"][aria-label="Your name"]')))
        except Exception:
            logger.info("Google Meet did not show an anonymous name input; continuing as signed-in/known user")
            return
        if self.humanized_input:
            self.humanized_input.click_element(name_input)
            self.humanized_input.copy_and_paste(self.config.bot_display_name)
        else:
            name_input.send_keys(self.config.bot_display_name)
        logger.info("Name input filled out")

    def _turn_off_media(self):
        for label in ("Turn off microphone", "Turn off camera"):
            try:
                button = WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable((By.CSS_SELECTOR, f'button[aria-label="{label}"], div[aria-label="{label}"]')))
            except Exception:
                logger.debug("Media control not available: %s", label)
                continue
            if self.humanized_input:
                self.humanized_input.click_element(button)
            else:
                button.click()

    def _save_artifacts(self, label):
        """Persist a screenshot, the DOM and the URL so a failed join can be diagnosed."""
        if not self.driver:
            return
        directory = Path(self.config.artifact_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prefix = f"{stamp}-{label}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.driver.save_screenshot(str(directory / f"{prefix}.png"))
            (directory / f"{prefix}.html").write_text(self.driver.page_source, encoding="utf-8")
            (directory / f"{prefix}.url.txt").write_text(self.driver.current_url, encoding="utf-8")
            logger.info("Saved failure artifacts to %s", directory / prefix)
        except Exception:
            logger.exception("Failed to save failure artifacts")

    def leave(self):
        if not self.driver:
            return
        try:
            self.driver.execute_script("window.ws?.disableMediaSending();")
            button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, LEAVE_CALL_SELECTOR)))
            button.click()
        except Exception:
            logger.debug("Google Meet leave button unavailable during shutdown")

    def close(self):
        self.stop_requested.set()
        try:
            self.leave()
        finally:
            self._quit_driver()
            self.websocket_server.close()
            if self.display:
                self.display.stop()
                self.display = None
