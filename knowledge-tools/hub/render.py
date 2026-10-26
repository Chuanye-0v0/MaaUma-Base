"""Render the same tool result into a local PNG card; no external image fetches."""
from pathlib import Path
import os
import textwrap
import uuid

TASK=Path(__file__).resolve().parents[1]

def _font(size,bold=False):
    from PIL import ImageFont
    choices=[os.environ.get('MAAUMA_KNOWLEDGE_FONT',''),r'C:\Windows\Fonts\msyhbd.ttc'if bold else r'C:\Windows\Fonts\msyh.ttc',
             '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    for path in choices:
        if path and Path(path).is_file():return ImageFont.truetype(path,size)
    return ImageFont.load_default(size=size)

def render_result(packet,*,path=None,title='MaaUma 资料与规划'):
    from PIL import Image,ImageDraw
    value=packet.get('result',packet)
    if isinstance(value,dict)and value.get('results'):
        responses=[row.get('response',{}).get('result',{})for row in value['results']]
        tree=next((row for row in reversed(responses)if isinstance(row,dict)and row.get('tree')),None)
    else:tree=value if isinstance(value,dict)and value.get('tree')else None
    image=Image.new('RGB',(1200,930),'#f5f7fb');draw=ImageDraw.Draw(image)
    title_font,normal,small=_font(32,True),_font(23),_font(18)
    draw.text((42,26),title,fill='#1e2d47',font=title_font)
    draw.text((44,80),'资料、机制与假设随结果记录',fill='#687892',font=small)
    if tree:
        positions={'target':(435,150,765,275),'parent_a':(180,355,510,480),'parent_b':(690,355,1020,480),
            'gp_a1':(30,610,300,750),'gp_a2':(320,610,590,750),'gp_b1':(610,610,880,750),'gp_b2':(900,610,1170,750)}
        for parent,child in (('target','parent_a'),('target','parent_b'),('parent_a','gp_a1'),('parent_a','gp_a2'),('parent_b','gp_b1'),('parent_b','gp_b2')):
            a,b=positions[parent],positions[child];x1=(a[0]+a[2])//2;x2=(b[0]+b[2])//2;mid=(a[3]+b[1])//2
            draw.line([(x1,a[3]),(x1,mid),(x2,mid),(x2,b[1])],fill='#afc1de',width=3)
        nodes={row['key']:row for row in tree['tree']['nodes']}
        colors={'owned':'#147a68','borrowable':'#0876a4','planned':'#a87514','unknown':'#7b8798'}
        labels={'owned':'已有','borrowable':'可借','planned':'待培养','unknown':'持有情况未知'}
        for key,box in positions.items():
            node=nodes.get(key,{});draw.rounded_rectangle(box,14,fill='#edf3ff'if key=='target'else'white',outline='#cbd8eb',width=2)
            draw.text((box[0]+16,box[1]+10),node.get('label')or key,fill='#687892',font=small)
            name=node.get('name')or'待选择'
            for index,line in enumerate(textwrap.wrap(name,width=10)[:2]):draw.text((box[0]+16,box[1]+38+index*28),line,fill='#1e2d47',font=normal)
            status=node.get('status','unknown');draw.text((box[0]+16,box[3]-30),labels.get(status,status),fill=colors.get(status,'#687892'),font=small)
        score=(tree.get('compatibility')or{}).get('total');known=(tree.get('compatibility')or{}).get('known_subtotal')
        draw.text((44,810),f'基础相性：{score if score is not None else "待补齐"}     已知小计：{known if known is not None else "—"}',fill='#1762f5',font=normal)
        draw.text((44,860),'基础相性、实际胜场和后续继承概率分开展示。',fill='#687892',font=small)
    else:
        text=value.get('text')or value.get('message')or str(value)if isinstance(value,dict)else str(value)
        y=145
        for paragraph in str(text).splitlines():
            for line in textwrap.wrap(paragraph,width=43)or['']:
                if y>830:break
                draw.text((48,y),line,fill='#1e2d47',font=normal);y+=37
            y+=7
        draw.text((44,875),'本地计算参考 · 具体区服、输入与来源见同份工具结果',fill='#687892',font=small)
    target=Path(path)if path else TASK/'exports/chat-cards'/(uuid.uuid4().hex+'.png')
    target.parent.mkdir(parents=True,exist_ok=True);image.save(target)
    return {'ok':True,'path':str(target),'width':image.width,'height':image.height,'external_images_requested':0}
