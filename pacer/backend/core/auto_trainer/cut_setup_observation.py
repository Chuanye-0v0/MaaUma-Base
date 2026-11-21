"""Read setup presentation without inferring card identity or action authority."""
import cv2
import numpy as np


SUPPORT_SLOT_BOXES=tuple((x,y,x+196,y+260) for y in (260,535) for x in (50,265,478))


def support_slot_observation(image,tokens=()):
    """Check each physical card artwork area, not the number of OCR labels.

    The middle of an empty slot is flat except for its plus sign. A populated
    card has distributed portrait/artwork detail. This observes occupancy
    only; IDs, levels, breaks and borrowed-card requirements stay elsewhere.
    """
    result={'schema_version':'uma.cut_support_slots.v1','owned_full':False,'full':False,'slots':[]}
    if not isinstance(image,np.ndarray) or image.shape[:2]!=(1280,720):return result
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    for slot,(x0,y0,x1,y1) in enumerate(SUPPORT_SLOT_BOXES):
        art=gray[y0+35:y1-35,x0+8:x1-8]
        detailed=[]
        for row,pieces in enumerate(np.array_split(art,3)):
            for col,cell in enumerate(np.array_split(pieces,3,axis=1)):
                low,high=np.quantile(cell,(.1,.9))
                edges=cv2.magnitude(cv2.Sobel(cell,cv2.CV_32F,1,0),cv2.Sobel(cell,cv2.CV_32F,0,1))
                if high-low>=15 and float((edges>25).mean())>=.08:detailed.append((row,col))
        occupied=len(detailed)>=6 and len({row for row,_ in detailed})>=2 and len({col for _,col in detailed})>=2
        level_seen=any(str(t.get('text','')).startswith('等级') and len(t.get('bbox',[]))==4
            and x0<=(t['bbox'][0]+t['bbox'][2])/2<x1 and y0<=(t['bbox'][1]+t['bbox'][3])/2<y1 for t in tokens)
        result['slots'].append({'slot':slot,'occupied':True if occupied else None,
            'basis':'distributed_card_artwork' if occupied else 'unreadable_or_empty',
            'detail_regions':len(detailed),'level_text_seen':level_seen})
    result['owned_full']=all(row['occupied'] is True for row in result['slots'][:5])
    result['full']=all(row['occupied'] is True for row in result['slots'])
    return result


def selected_mode_label(image,token):
    """Selected tabs have light lettering on a filled tab; no hue threshold.

    Compare the label to its immediate left/right tab background. Reading
    both captions is insufficient: a flat or occluded label stays unknown.
    """
    if not isinstance(image,np.ndarray) or image.shape[:2]!=(1280,720):return False
    x0,y0,x1,y1=map(round,token['bbox'])
    if not (0<=x0<x1<=720 and 0<=y0<y1<=1280) or y1-y0<10:return False
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    inset=max(2,min(4,(y1-y0)//5));top,bottom=y0+inset,y1-inset
    glyphs=gray[top:bottom,x0:x1]
    sides=[gray[top:bottom,max(0,x0-32):max(0,x0-8)],gray[top:bottom,min(720,x1+8):min(720,x1+32)]]
    samples=[side.ravel() for side in sides if side.size]
    if not glyphs.size or not samples:return False
    background=float(np.median(np.concatenate(samples)));dark,light=np.quantile(glyphs,(.1,.9))
    bright_contrast=float(light-background);dark_contrast=max(0.,float(background-dark))
    return bright_contrast>=16 and bright_contrast>dark_contrast*1.15
