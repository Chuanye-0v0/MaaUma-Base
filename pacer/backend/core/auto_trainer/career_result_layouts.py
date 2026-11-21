"""Shared result-page controls, independent of rating prose and badge palette."""
import cv2
import numpy as np


def title_notice_confirm_point(image):
    """Known earned-title panel's lone green footer, independent of OCR punctuation."""
    if image is None or image.ndim != 3 or image.shape[1] >= image.shape[0]:
        return None
    height,width=image.shape[:2]
    hsv=cv2.cvtColor(cv2.resize(image,(720,1280)),cv2.COLOR_BGR2HSV)
    green=(hsv[:,:,0]>=30)&(hsv[:,:,0]<=95)&(hsv[:,:,1]>90)&(hsv[:,:,2]>100)
    white=(hsv[:,:,1]<75)&(hsv[:,:,2]>195)
    if (float(green[35:92,25:695].mean())>.80
            and float(white[105:995,16:704].mean())>.50
            and float(green[1148:1212,230:490].mean())>.72):
        return round(360*width/720),round(1183*height/1280)
    return None


def diagonal_goal_band(top_band, image_height):
    green = ((top_band[:, :, 0] >= 38) & (top_band[:, :, 0] <= 92)
        & (top_band[:, :, 1] > 70) & (top_band[:, :, 2] > 80))
    width = green.shape[1]
    left = np.nonzero(green[:, :max(1, int(width * .15))])[0]
    right = np.nonzero(green[:, int(width * .85):])[0]
    return bool(left.size and right.size and abs(float(left.mean()) - float(right.mean())) > image_height * .02)


def exchange_history_continue_point(image):
    """Wide blue/grey exchange rows, not the large final rating badge."""
    if image is None or image.ndim != 3 or image.shape[1] >= image.shape[0]:
        return None
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    button = hsv[1084:1138, 234:486]
    green = ((button[:, :, 0] >= 35) & (button[:, :, 0] <= 95)
        & (button[:, :, 1] > 90) & (button[:, :, 2] > 90))
    if float(green.mean()) < .75:
        return None
    blue = ((hsv[:, :, 0] >= 100) & (hsv[:, :, 0] <= 130)
        & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] >= 95) & (hsv[:, :, 2] < 225)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(blue)
    rows = []
    for x,y,w,h,area in stats[1:]:
        if not (x < 65 and w > 585 and 250 < y < 945 and 35 <= h <= 80 and area/(w*h) > .75):
            continue
        body = hsv[y+h+5:min(980,y+h+65), x+15:min(710,x+w-15)]
        grey = ((body[:, :, 1] < 45) & (body[:, :, 2] > 80) & (body[:, :, 2] < 220))
        if body.size and float(grey.mean()) > .65:
            rows.append(y)
    if not 1 <= len(rows) <= 4:
        return None
    return round(360*width/720), round(1110*height/1280)


def rating_continue_point(image):
    if image is None or image.ndim != 3:
        return None
    height, width = image.shape[:2]
    if width >= height or height < 600 or width < 320:
        return None
    if exchange_history_continue_point(image) is not None:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    def green_ratio(region):
        return float(np.mean((region[:, :, 0] >= 35) & (region[:, :, 0] <= 95)
            & (region[:, :, 1] > 75) & (region[:, :, 2] > 95)))
    header = hsv[:int(height * .09)]
    diagonal = hsv[int(height * .77):int(height * .88)]
    button = hsv[int(height * .89):int(height * .96), int(width * .31):int(width * .69)]
    badge = hsv[int(height * .15):int(height * .47), int(width * .16):int(width * .84)]
    if (green_ratio(header) > .25 and green_ratio(diagonal) > .35
            and green_ratio(button) > .65
            and float(np.mean((badge[:, :, 1] > 65) & (badge[:, :, 2] > 100))) > .30):
        return int(width * .5), int(height * .92)
    return None
