"""Small UI-only wakeups, never purchase or outing authorization."""
import time
import cv2
import numpy as np
from core.execution_frames import wait_for_frame


def canonical(image):
    return cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)


def modal_content_pending(image):
    im = canonical(image)
    return _modal_content_pending(im, cv2.cvtColor(im, cv2.COLOR_BGR2HSV))


def _modal_content_pending(im, hsv):
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    green = (hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
    # The full-height aptitude confirmation paints its buttons and blank body
    # before its header. Do not let the missing header classify it as a page.
    if (green[35:85, 30:690].mean() < .2
            and _white_ratio(hsv[350:960, 80:640], 35, 210) > .98
            and _white_ratio(hsv[1135:1190, 85:325], 35, 210) > .7
            and green[1135:1190, 400:635].mean() > .7):
        return True
    if not (SafeAdvancePlanner._looks_like_confirm_modal_panel(im) or _modal_controls(hsv)):
        return False
    rows = np.flatnonzero(green[:850, 30:690].mean(axis=1) > .7)
    # A nested confirmation can leave the chooser header visible behind it.
    # Its lower, foreground header owns the body we must wait for.
    for band in reversed(np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1)):
        if 20 <= len(band) <= 100:
            end = int(band[-1])
            # A short confirmation's buttons begin only ~270 px below its
            # header. Do not mistake their labels or outer border for body text.
            body = cv2.cvtColor(im[end+20:min(end+220, 1000), 120:600], cv2.COLOR_BGR2GRAY)
            return bool(np.mean(body < 180) < .005)
    return False


def controls_changed(before, after):
    if before is None:
        return True
    a, b = canonical(before), canonical(after)
    # Opaque UI region, excluding the animated upper character/background.
    diff = cv2.absdiff(a[650:1120, 30:690], b[650:1120, 30:690])
    return float(np.mean(np.max(diff, axis=2) > 24)) > .0003


def adaptation_modal_content_pending(image):
    """Also recognize the upgrade shell during its opening zoom, without OCR."""
    im = canonical(image)
    return _adaptation_modal_content_pending(im, cv2.cvtColor(im, cv2.COLOR_BGR2HSV))


def _adaptation_modal_content_pending(im, hsv):
    if _modal_content_pending(im, hsv):
        return True
    green = (hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
    # The title/buttons can be opaque while the selected item headings are
    # still translucent. Their missing text/strong-blue bands are loading,
    # not a different purchase. This only delays the existing strict check.
    edge = hsv[:1060, 620:655]
    pale_blue = ((edge[:, :, 0] > 100) & (edge[:, :, 0] < 130)
        & (edge[:, :, 1] >= 25) & (edge[:, :, 1] <= 100) & (edge[:, :, 2] > 190))
    rows = np.flatnonzero(pale_blue.mean(axis=1) > .8)
    bands = np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1)
    body_white = _white_ratio(hsv[600:1030, 60:660], 45, 200)
    if (any(50 <= len(band) <= 100 for band in bands)
            and green[1135:1202, 400:635].mean() > .7
            and body_white > .75):
        return True
    # At the beginning of the zoom the buttons sit above their final slots,
    # the body is translucent, and the title has not rendered yet. This is
    # only a read-only wakeup condition for an already owned adaptation flow.
    visible_header = np.count_nonzero(green[:1000, 30:690].mean(axis=1) > .7) >= 20
    return bool(not visible_header
        and body_white > .75
        and _white_ratio(hsv[1050:1220, 55:345], 45, 200) > .22
        and green[1050:1220, 375:665].mean() > .2)


def ss_preview_controls(image):
    """Wake the normal preview reader; this does not authorize an SS action."""
    hsv = cv2.cvtColor(canonical(image), cv2.COLOR_BGR2HSV)
    ribbon = hsv[1090:1175, 606:702]
    gold = ((ribbon[:, :, 0] >= 15) & (ribbon[:, :, 0] <= 38)
            & (ribbon[:, :, 1] > 100) & (ribbon[:, :, 2] > 150)).astype(np.uint8)
    _, _, components, _ = cv2.connectedComponentsWithStats(gold, connectivity=8)
    chevrons = [row for row in components[1:]
                if row[cv2.CC_STAT_WIDTH] >= 48 and 12 <= row[cv2.CC_STAT_HEIGHT] <= 35
                and row[cv2.CC_STAT_AREA] >= 250]
    points_panel = hsv[695:730, 375:650]
    blue = ((points_panel[:, :, 0] >= 95) & (points_panel[:, :, 0] <= 125)
            & (points_panel[:, :, 1] > 90) & (points_panel[:, :, 2] > 120))
    return bool(blue.mean() > .3
                and any(row[cv2.CC_STAT_TOP] < 28 for row in chevrons)
                and any(row[cv2.CC_STAT_TOP] > 40 for row in chevrons))


def outing_controls(image, *, home=False, confirm=False):
    from core.auto_trainer.safe_advance import SafeAdvanceExecutor, SafeAdvancePlanner
    im = canonical(image)
    if home:
        from core.auto_trainer.larc_pages import larc_race_day_controls
        return SafeAdvanceExecutor._career_home_controls_ready(im) or larc_race_day_controls(im)
    # The modal shell/buttons arrive before its content. A blank shell must
    # not wake the reader, which needs the chooser/confirmation body.
    if modal_content_pending(im):
        return False
    if confirm:
        # During the opening zoom, the small header can already span most of
        # the ROI while the actual confirmation buttons are still absent.
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        green = (hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
        if (np.count_nonzero(green[380:445, 30:690].mean(axis=1) > .85) < 20
                or green[795:875, 375:665].mean() < .25):
            return False
    else:
        # The chooser shell can be complete while its name/arrows still fade
        # from white. Wait for ordinary brown body text, without another OCR.
        hsv = cv2.cvtColor(im[390:500, 150:340], cv2.COLOR_BGR2HSV)
        brown = (hsv[:, :, 0] < 35) & (hsv[:, :, 1] > 60) & (hsv[:, :, 2] < 160)
        if float(brown.mean()) < .02:
            return False
    return SafeAdvancePlanner._looks_like_confirm_modal_panel(im) or modal_controls(im)


def modal_controls(image):
    return _modal_controls(cv2.cvtColor(canonical(image), cv2.COLOR_BGR2HSV))


def _white_ratio(hsv, max_saturation, min_value):
    return np.mean((hsv[:, :, 1] < max_saturation) & (hsv[:, :, 2] > min_value))


def _modal_controls(hsv):
    if _white_ratio(hsv[550:770, 40:680], 25, 200) < .55:
        return False
    header = hsv[:900, 30:690]
    green = (header[:, :, 0] > 30) & (header[:, :, 0] < 85) & (header[:, :, 1] > 90) & (header[:, :, 2] > 120)
    rows = np.flatnonzero(green.mean(axis=1) > .7)
    for band in np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1):
        if not 20 <= len(band) <= 100:
            continue
        end = int(band[-1])
        if _white_ratio(hsv[end+15:min(1200, end+530), 30:690], 65, 175) > .5:
            return True
    return False


def adaptation_controls(image, *, allow_home=True, require_leave_grid=False):
    from core.auto_trainer.safe_advance import SafeAdvanceExecutor, SafeAdvancePlanner
    im = canonical(image)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    if _adaptation_modal_content_pending(im, hsv):
        return False
    from core.auto_trainer.larc_pages import larc_race_day_controls
    if SafeAdvanceExecutor._career_home_controls_ready(im) or larc_race_day_controls(im):
        return allow_home
    if SafeAdvancePlanner._looks_like_confirm_modal_panel(im) or _modal_controls(hsv):
        return True
    if require_leave_grid:
        return False
    # Existing aptitude grid's opaque blue details header and panel.
    header = hsv[655:682, 50:670]
    blue = (header[:, :, 0] > 100) & (header[:, :, 0] < 130) & (header[:, :, 1] > 100)
    return bool(blue.mean() > .65 and _white_ratio(hsv[775:965, 60:660], 65, 175) > .25)


def wait_outing_controls(adb, before, *, home=False, confirm=False, event_handoff=False, sleep=time.sleep, clock=time.monotonic):
    if event_handoff:
        from scripts.larc_transition_probe import actionable_surface
    result = wait_for_frame(adb,
        lambda frame: controls_changed(before, frame) and (outing_controls(frame, home=home, confirm=confirm)
            or event_handoff and actionable_surface(frame) == "event_choice"),
        timeout=1.2, max_polls=12, initial_delay=True, sleep=sleep, clock=clock)
    return result.frame if result.value else None
