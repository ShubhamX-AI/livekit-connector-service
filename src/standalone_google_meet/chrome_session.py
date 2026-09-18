import json
import logging
import os
import time
from pathlib import Path

from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .config import ConnectorConfig
from .websocket_server import BrowserWebSocketServer

logger = logging.getLogger(__name__)
ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"
JOIN_ATTEMPTS = 3
JOIN_RETRY_DELAY_SECONDS = 5
JOIN_BLOCK_CHECK_SECONDS = 8
BLOCKED_TEXTS = ("You can't join this video call", "There is a problem connecting to this video call")


class GoogleBlockingJoinError(RuntimeError):
    """Google rejected the join attempt with its generic block screen."""


class GoogleMeetChromeSession:
    def __init__(self, config: ConnectorConfig, livekit_sync):
        self.config = config
        self.livekit_sync = livekit_sync
        self.display = None
        self.driver = None
        self.websocket_server = BrowserWebSocketServer(
            host=config.websocket_host,
            port=config.websocket_port,
            livekit_sync=livekit_sync,
            upstream_livekit_url=config.livekit_url,
        )

    def start(self):
        self._start_display()
        self.websocket_server.start()
        self._start_driver()
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
        options.add_argument("--window-size=1280,720")
        options.add_argument("--start-fullscreen")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        if self.config.headless:
            options.add_argument("--headless=new")
        if os.environ.get("ENABLE_CHROME_SANDBOX", "false").lower() != "true":
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-setuid-sandbox")

        self.driver = webdriver.Chrome(options=options, service=Service(executable_path=self.config.chrome_driver_path))
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": self._browser_script()})

    def _browser_script(self):
        initial_data = {
            "websocketPort": self.config.websocket_port,
            "videoFrameWidth": 1280,
            "videoFrameHeight": 720,
            "botName": self.config.bot_display_name,
            "addClickRipple": False,
            "recordingView": "speaker_view",
            "sendMixedAudio": False,
            "sendPerParticipantAudio": True,
            "perParticipantRealtimeVideoConfiguration": {
                "webcam_configuration": {"enabled": False},
                "screenshare_configuration": {"enabled": False},
            },
            "roomSyncSourceParticipantConfiguration": self.livekit_sync.source_browser_config(self.config.source_selector),
            "sendPerParticipantVideo": False,
            "collectCaptions": False,
            "recordParticipantSpeechStartStopEvents": False,
        }
        script_parts = [f"window.initialData = {json.dumps(initial_data)};", "window.googleMeetInitialData = {modifyDomForVideoRecording: false, disableIncomingVideo: true};"]
        for filename in ("protobuf.min.js", "pako.min.js", "livekit-client.umd.min.js", "livekit-client-adapter.js", "shared_chromedriver_payload.js", "google_meet_chromedriver_payload.js"):
            script_parts.append((ASSET_DIR / filename).read_text(encoding="utf-8"))
        return "\n".join(script_parts)

    def _join_meeting(self):
        # Google sometimes rejects an anonymous join with "You can't join this video call"
        # right after the join click. That block is often transient, so retry the whole
        # join flow a few times before giving up.
        last_error = None
        for attempt in range(1, JOIN_ATTEMPTS + 1):
            logger.info("Opening Google Meet (attempt %s/%s): %s", attempt, JOIN_ATTEMPTS, self.config.meeting_url)
            try:
                self._attempt_join()
                logger.info("Google Meet join requested")
                return
            except GoogleBlockingJoinError as error:
                last_error = error
                logger.warning("Google blocked the join attempt: %s", error)
                if attempt < JOIN_ATTEMPTS:
                    time.sleep(JOIN_RETRY_DELAY_SECONDS * attempt)
        raise RuntimeError(f"Google blocked every join attempt ({JOIN_ATTEMPTS}). Joining as a signed-in Google account usually avoids this.") from last_error

    def _attempt_join(self):
        self.driver.get(self.config.meeting_url)
        self.driver.execute_cdp_cmd("Browser.grantPermissions", {"origin": self.config.meeting_url, "permissions": ["audioCapture", "videoCapture"]})
        self._reject_invalid_meeting()
        self._raise_if_blocked()
        self._fill_name()
        self._turn_off_media()
        join_button = WebDriverWait(self.driver, 60).until(
            EC.presence_of_element_located((By.XPATH, '//button[.//span[text()="Ask to join" or text()="Ask to join anyway" or text()="Join now" or text()="Join the call now" or text()="Join anyway" or text()="Join here too"]]'))
        )
        join_button.click()
        self._wait_until_join_accepted()
        self.driver.execute_script("window.ws?.enableMediaSending();")

    def _wait_until_join_accepted(self):
        # The block screen shows up within a few seconds of the click. Anything else means the
        # request went through and the bot is either in the call or in the waiting room.
        deadline = time.monotonic() + JOIN_BLOCK_CHECK_SECONDS
        while time.monotonic() < deadline:
            self._raise_if_blocked()
            time.sleep(1)

    def _raise_if_blocked(self):
        elements = self.driver.find_elements(By.XPATH, "//*[" + " or ".join(f'contains(text(), "{text}")' for text in BLOCKED_TEXTS) + "]")
        for element in elements:
            if element.is_displayed():
                raise GoogleBlockingJoinError(element.text.strip().splitlines()[0])

    def _reject_invalid_meeting(self):
        invalid_texts = ("Check your meeting code", "Invalid video call name", "Your meeting code has expired")
        body = WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        if any(text in body.text for text in invalid_texts):
            raise RuntimeError("Google Meet URL is invalid or expired")

    def _fill_name(self):
        try:
            name_input = WebDriverWait(self.driver, 30).until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="text"][aria-label="Your name"]')))
            name_input.send_keys(self.config.bot_display_name)
        except Exception:
            logger.info("Google Meet did not show an anonymous name input; continuing as signed-in/known user")

    def _turn_off_media(self):
        for label in ("Turn off microphone", "Turn off camera"):
            try:
                button = WebDriverWait(self.driver, 15).until(EC.element_to_be_clickable((By.CSS_SELECTOR, f'button[aria-label="{label}"], div[aria-label="{label}"]')))
                button.click()
            except Exception:
                logger.debug("Media control not available: %s", label)

    def leave(self):
        if not self.driver:
            return
        try:
            self.driver.execute_script("window.ws?.disableMediaSending();")
            button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[jsname="CQylAd"][aria-label="Leave call"]')))
            button.click()
        except Exception:
            logger.debug("Google Meet leave button unavailable during shutdown")

    def close(self):
        try:
            self.leave()
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    logger.exception("Failed to close Chrome")
                self.driver = None
            self.websocket_server.close()
            if self.display:
                self.display.stop()
                self.display = None
