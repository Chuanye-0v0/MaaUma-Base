"""Current-frame labels for a verified information page; unknown layouts fall back."""
import cv2


def information_text(image, navigation, *, reader=None):
    if (not navigation.get('page_verified') or navigation.get('close') is None
            or navigation.get('information',{}).get('status')!='accepted'):
        return None
    from core.auto_trainer.larc_preview_text import _frame, _reader, _line
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    from core.auto_trainer.larc_control_wait import modal_content_pending
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    image=_frame(image)
    # This full-height information card itself has one modal close control.
    # Reject overlaid confirmation panels, not its expected bottom close.
    if (image is None or connecting_overlay_visible(image) or modal_content_pending(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image)):
        return None
    # An overlay/dimmed background must not pass just because normalized
    # template correlation still recognizes the old title beneath it.
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    if ((hsv[40:85,120:580,1]>100)&(hsv[40:85,120:580,2]>190)).mean()<.55:
        return None
    try:
        read=_reader(reader)
        title=_line(image,(170,30,550,97),read)
        # Tighten only the known close-label rectangle. Wide empty margins in
        # recorded/resized frames are recognized as spaces with low confidence.
        label=image[1148:1228,250:470]
        colors=cv2.cvtColor(label,cv2.COLOR_BGR2HSV)
        points=cv2.findNonZero(((colors[:,:,0]>=8)&(colors[:,:,0]<40)
            &(colors[:,:,1]>45)&(colors[:,:,2]<200)).astype('uint8'))
        if points is None:return None
        x,y,w,h=cv2.boundingRect(points)
        if w<25 or h<15:return None
        close=_line(label,(max(0,x-2),max(0,y-2),min(label.shape[1],x+w+2),min(label.shape[0],y+h+2)),read)
        if title!='群星赛信息' or close!='关闭':return None
        return title+'\n'+close
    except (OSError,RuntimeError,ValueError,TypeError,cv2.error):
        return None
