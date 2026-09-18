"""Human-like mouse and keyboard input for the Google Meet join flow.

Google rejects anonymous joins that are driven by WebDriver's synthetic clicks
and `send_keys` with a generic "You can't join this video call" screen. Real
X11 pointer motion along a generated reaching trajectory, plus keyboard input
delivered through XTEST, gets past that. This mirrors the "humanized"
interaction mode of the parent repository
(`bots/google_meet_bot_adapter/google_meet_ui_methods.py`).
"""

import logging
import random
import subprocess
import time

from .mocap_manager import MocapManager
from .x11_input import X11Input

logger = logging.getLogger(__name__)

UNSHIFTED_PUNCTUATION = {
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "`": "grave",
}

SHIFTED_PUNCTUATION = {
    "~": "grave",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "minus",
    "+": "equal",
    "{": "bracketleft",
    "}": "bracketright",
    "|": "backslash",
    ":": "semicolon",
    '"': "apostrophe",
    "<": "comma",
    ">": "period",
    "?": "slash",
}

ELEMENT_METRICS_SCRIPT = """
const el = arguments[0];
const r = el.getBoundingClientRect();
return {
    left: r.left,
    top: r.top,
    width: r.width,
    height: r.height,
    screenX: window.screenX,
    screenY: window.screenY,
    dpr: window.devicePixelRatio || 1
};
"""

ELEMENT_AT_POINT_SCRIPT = """
var el = document.elementFromPoint(arguments[0], arguments[1]);
var expected = arguments[2];
return !!el && (el === expected || expected.contains(el));
"""

SEQUENCE_ATTEMPTS = 10


class HumanizedInputUnavailableError(RuntimeError):
    """No usable pointer trajectory reaches the target element."""


class HumanizedInput:
    def __init__(self, driver, video_frame_size=(1280, 720)):
        self.driver = driver
        self.x11_input = X11Input()
        self.mocap_manager = MocapManager(video_frame_size=video_frame_size)
        # Counts dead ends so that repeated failures allow the trajectory
        # generator to stretch and rotate its path to reach the target.
        self.sequence_unavailable_count = 0

    def position_mouse(self):
        """Move the pointer somewhere plausible before the page loads.

        Without this the pointer sits at (0, 0) for the whole session, which no
        real user's pointer ever does.
        """
        position = self.mocap_manager.get_initial_mouse_position()
        if position is None:
            return
        self.x11_input.move_abs(*position)
        logger.info("Positioned mouse at %s", position)

    def type_text(self, text):
        for index, char in enumerate(text):
            if index == 0:
                time.sleep(random.uniform(0.15, 0.35))
            self._type_char(char)
            time.sleep(random.uniform(0.24, 0.48))

    def copy_and_paste(self, text):
        """Paste via the X clipboard, which is how a human fills a name field."""
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True)
        time.sleep(random.uniform(0.15, 0.35))
        self.x11_input.key_press("Control")
        self.x11_input.key_press("v")
        self.x11_input.key_release("v")
        self.x11_input.key_release("Control")

    def click_element(self, element):
        rect = self._screen_rect(element)
        pointer = self.x11_input.root.query_pointer()._data
        current_x = int(pointer["root_x"])
        current_y = int(pointer["root_y"])

        logger.info("Humanized click: pointer at (%s,%s), target rect %s", current_x, current_y, rect["screen"])
        sequence = self._find_sequence_landing_on(element, current_x, current_y, rect)
        logger.info("Humanized click: %s movements, dx=%s dy=%s", len(sequence.movements), sequence.total_dx, sequence.total_dy)

        for movement in sequence.movements:
            delay = movement.get("dt", 0)
            if delay > 0:
                time.sleep(delay)
            dx = movement.get("dx", 0)
            dy = movement.get("dy", 0)
            if dx or dy:
                self.x11_input.move_rel(dx, dy)

        if sequence.click_down_dt > 0:
            time.sleep(sequence.click_down_dt)
        self.x11_input.button_press("left")
        if sequence.click_up_dt > 0:
            time.sleep(sequence.click_up_dt)
        self.x11_input.button_release("left")

    def _type_char(self, char):
        needs_shift = char.isupper() or char in SHIFTED_PUNCTUATION
        if char in SHIFTED_PUNCTUATION:
            base = SHIFTED_PUNCTUATION[char]
        elif char in UNSHIFTED_PUNCTUATION:
            base = UNSHIFTED_PUNCTUATION[char]
        elif char.isupper():
            base = char.lower()
        else:
            base = char

        if needs_shift:
            self.x11_input.key_press("Shift")
        self.x11_input.key_press(base)
        self.x11_input.key_release(base)
        if needs_shift:
            self.x11_input.key_release("Shift")

    def _screen_rect(self, element):
        metrics = self.driver.execute_script(ELEMENT_METRICS_SCRIPT, element)
        if not metrics:
            raise HumanizedInputUnavailableError("No metrics returned for target element")

        width = float(metrics["width"])
        height = float(metrics["height"])
        if width <= 0 or height <= 0:
            raise HumanizedInputUnavailableError(f"Element has invalid size: {width}x{height}")

        left = float(metrics["left"])
        top = float(metrics["top"])
        screen_x = float(metrics["screenX"])
        screen_y = float(metrics["screenY"])
        dpr = float(metrics["dpr"])

        return {
            "dpr": dpr,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "screen": (
                int(round((screen_x + left) * dpr)),
                int(round((screen_y + top) * dpr)),
                int(round((screen_x + left + width) * dpr)),
                int(round((screen_y + top + height) * dpr)),
            ),
        }

    def _find_sequence_landing_on(self, element, current_x, current_y, rect):
        rect_left, rect_top, rect_right, rect_bottom = rect["screen"]
        for attempt in range(SEQUENCE_ATTEMPTS):
            sequence = self.mocap_manager.find_random_sequence_landing_in_rect(current_x, current_y, rect_left, rect_top, rect_right, rect_bottom)

            if sequence is None and self.sequence_unavailable_count > 1:
                logger.warning("No direct trajectory reaches the target; allowing stretch and rotation")
                sequence = self.mocap_manager.find_random_sequence_landing_in_rect_with_stretch_and_rotation_allowed(current_x, current_y, rect_left, rect_top, rect_right, rect_bottom)

            if sequence is None:
                self.sequence_unavailable_count += 1
                raise HumanizedInputUnavailableError(f"No trajectory lands inside {rect['screen']} from ({current_x},{current_y})")

            page_x = (current_x + sequence.total_dx) / rect["dpr"] - rect["screen_x"]
            page_y = (current_y + sequence.total_dy) / rect["dpr"] - rect["screen_y"]
            if self.driver.execute_script(ELEMENT_AT_POINT_SCRIPT, page_x, page_y, element):
                return sequence

            logger.info("Humanized click: endpoint (%.1f, %.1f) is not on the target, retrying (%s/%s)", page_x, page_y, attempt + 1, SEQUENCE_ATTEMPTS)

        raise HumanizedInputUnavailableError(f"No trajectory landed on the target element after {SEQUENCE_ATTEMPTS} attempts")
