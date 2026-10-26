"""Local language routing to real tools, shared by the browser and QQ adapter."""
import re

REFERENCE_STATS={'speed':1200,'stamina':1000,'power':1000,'guts':800,'wisdom':1000}

def dispatch(request,*,storage_root=None):
    from hub.api import dispatch as call
    text=str(request.get('text')or'').strip();context=request.get('context')or{}
    server='jp'if'日服'in text else'tw'if'台服'in text or'繁中'in text else str(request.get('server')or context.get('server')or'cn')
    outputs=[]
    def tool(action,**fields):
        packet=call({'action':action,'server':server,**fields},storage_root=storage_root)
        outputs.append({'action':action,'response':packet})
        return packet.get('result',{})if packet.get('ok')else{'ok':False,'error':packet.get('error')or packet.get('result',{}).get('error')}
    def finish(message,**extra):return {'ok':True,'text':message,'results':outputs,'router':'local_tools','server':server,**extra}
    if not text:return finish('可以问：查技能 弯道恢复、樱花桂冠日文名、泥地E到A缺几星，或京都3000米某技能收益。')
    if any(word in text for word in ('有什么','能做什么','帮助','资料库')):
        value=tool('knowledge.summary')
        return finish('可以查询技能、三服对应名、角色衣装与支援来源；计算七节点基础相性、补祖、红因子缺星和SP；也可以按赛道与面板模拟技能参考收益。资料和规则版本会随结果列出。',summary=value)
    apt=re.search(r'(?:泥地|草地|短距离|英里|中距离|长距离)?\s*([A-GS])\s*(?:到|→|->|升到)\s*([A-GS])',text,re.I)
    if apt and any(word in text for word in ('星','适性','泥地','草地')):
        value=tool('aptitude.gap',baseline_rank=apt[1].upper(),target_rank=apt[2].upper(),known_stars=0)
        amount=value.get('missing_stars',value.get('required_stars',value.get('minimum_stars')))
        if amount is None:
            plan=value.get('plan')or value.get('result')or{}
            amount=plan.get('missing_stars',plan.get('required_stars'))
        message=f'{apt[1].upper()} → {apt[2].upper()} 的开局红因子需求已计算。'
        if amount is not None:message+=f'当前按0星输入，还需约 {amount} 星。'
        message+='开局适性上限和育成中继承分别计算，概率不冒充确定结果。'
        return finish(message)
    if any(word in text for word in ('日文叫什么','日文名','繁中名','台服叫什么','译名','三服名')):
        query=re.sub(r'(日文叫什么|日文名|繁中名|台服叫什么|译名|三服名|查角色|叫什么|日服|国服|繁中服|简中服)','',text).strip(' ：:?？')
        value=tool('entity.names',query=query)
        entity=value.get('entity')or(value.get('items')or[{}])[0]
        names=entity.get('names')or value.get('names')or{}
        if not names:
            skills=tool('skill.search',query=query,limit=5)
            choices=skills.get('items')or[]
            if choices:
                names=choices[0].get('names')or{}
        if names:
            lines=[f'{query} 的对应名：']
            for key,name in names.items():
                if isinstance(name,dict):name=name.get('name')or name.get('value')or str(name)
                elif isinstance(name,list):name=' / '.join(str(row.get('name',row))if isinstance(row,dict)else str(row)for row in name)
                label={'zh_cn':'简中','zh_tw':'繁中','ja':'日文','en':'英文'}.get(key,key)
                lines.append(f'{label}：{name}')
            return finish('\n'.join(lines)+'\n正式主名和社区参考名保留各自来源。')
        return finish('已查询对应名；当前结果或候选见下方。缺少的译名不会用繁简转换冒充。')
    pair=re.findall(r'(?<!\d)(1\d{3})(?!\d)',text)
    if '相性'in text and len(pair)>=2:
        keys=('target','parent_a','gp_a1','gp_a2','parent_b','gp_b1','gp_b2')
        nodes=dict(zip(keys,[int(item)for item in pair[:7]]))
        value=tool('parents.evaluate',nodes=nodes)
        score=(value.get('compatibility')or{}).get('total');known=(value.get('compatibility')or{}).get('known_subtotal')
        return finish(f'当前七节点基础相性：{score if score is not None else "资料未补齐"}。已知分项小计：{known if known is not None else "待确认"}。共同胜场与继承概率另列，不混进这个基础分。')
    if 'SP'in text.upper()or'技能点'in text:
        query=re.sub(r'(?:SP|sp|技能点|多少钱|要多少|切者|切れ者|启发\s*\d|提示\s*\d|计算|查技能|查|国服|日服|台服)','',text).strip(' ：:?？')
        found=tool('skill.search',query=query,limit=5);items=found.get('items')or[]
        if not items:return finish('暂未匹配到明确技能。可以写“弯道恢复 SP 启发3 切者”，或在SP页加入多项技能计算。')
        level=re.search(r'(?:启发|提示)\s*(\d)',text);item=items[0]
        hint_level=int(level[1]) if level else 0
        value=tool('sp.quote',skills=[{'skill_id':str(item.get('db_id')or item.get('id')),'hint_level':hint_level}],cutting_edge='切者'in text or'切れ者'in text)
        total=value.get('total')or{};low,high=total.get('low'),total.get('high')
        price='费用待确认'if low is None else f'{low} SP'if low==high else f'{low}～{high} SP'
        return finish(f'{item.get("name",query)}：{price}。按启发{hint_level}计算；原生固有、继承技和升级链以所选条目为准。')
    is_sim=any(word in text for word in ('马身','收益','模拟','耐力够','耐力够不够'))
    query=re.sub(r'^(?:/马娘|/uma|查技能|查询技能|技能|查)\s*','',text)
    query=re.sub(r'(日服|国服|台服|繁中服|简中服)','',query)
    if is_sim:
        course_query='京都 3000'if'菊花赏'in text else None
        location=re.search(r'(京都|东京|東京|中山|阪神|中京|新潟|大井|札幌|函馆|函館|福岛|福島|小仓|小倉)[^\d]{0,8}(\d{3,4})',text)
        if location:course_query=location[1]+' '+location[2]
        query=re.sub(r'(菊花赏|菊花賞|京都\s*\d+\s*(?:米|m)?|东京\s*\d+\s*(?:米|m)?|中山\s*\d+\s*(?:米|m)?|收益是多少|收益|值几马身|多少马身|能快几马身|模拟|请问|帮我算|算一下|是多少|固有)','',query).strip(' ，,：:?？')
        found=tool('skill.search',query=query,limit=8);items=found.get('items')or[]
        if not items:return finish('先匹配具体技能，再做赛道计算。当前还没找到唯一条目；可以写“京都3000 直线巧者收益”，或在赛道页选择原生／继承版本。')
        selected=next((row for row in items if ('inherit'in str(row.get('kind'))or'继承'in str(row.get('kind')))==('继承'in text)),items[0])
        match=tool('simulation.skills',ids=[str(selected.get('db_id')or selected.get('id'))],ruleset=context.get('ruleset','jp'),limit=8)
        if not(match.get('skills')or match.get('items')):return finish('技能资料已查到，但当前参考模拟快照尚未覆盖这一条。可以先看条件和效果，不会补造马身数字。')
        courses=tool('simulation.courses',query=course_query or'京都 3000',ruleset=context.get('ruleset','jp'),limit=10)
        choices=courses.get('courses')or courses.get('items')or[]
        if not choices:return finish('技能资料已查到；赛道还需选择，可到“赛道与技能收益”页搜索。')
        course=next((row for row in choices if int(row.get('distance',0))==3000),choices[0])
        stats={**REFERENCE_STATS,**(context.get('stats')or{})}
        for field,word in (('speed','速度|速'),('stamina','耐力|耐'),('power','力量|力'),('guts','毅力|根性|根'),('wisdom','智力|智')):
            number=re.search(r'(?:'+word+r')\s*(\d{2,4})',text)
            if number:stats[field]=int(number[1])
        strategy='oikomi'
        if any(word in text for word in ('领跑','逃马','大逃','跑法逃')):strategy='nige'
        elif any(word in text for word in ('先行','前列','先马')):strategy='senkou'
        elif any(word in text for word in ('居中','差马','跑法差')):strategy='sashi'
        value=tool('simulation.run',course_id=course.get('id')or course.get('course_id'),stats=stats,strategy=strategy,
            compare_skill_id=str(selected.get('db_id')or selected.get('id')),samples=32,seed=20261025,ruleset=context.get('ruleset','jp'))
        delta=(value.get('skill_delta')or{}).get('bashin')or{}
        if delta.get('mean')is None:return finish('工具未返回可解释的收益数值。技能资料和已识别赛道仍保留，具体信息见工具结果。')
        return finish(f'按当前参考模型，{selected.get("name",query)}的平均收益约 {delta["mean"]:.3f} 马身（32样本，固定种子）。\n面板：速{stats["speed"]}／耐{stats["stamina"]}／力{stats["power"]}／根{stats["guts"]}／智{stats["wisdom"]}。未提供的项目采用上面的参考值。\n这是当前模型的技能对照，不是完整马群胜率；采用的区服、机制和触发分支见结果。')
    found=tool('skill.search',query=query.strip(' ：:?？'),limit=6);items=found.get('items')or[]
    if items:
        lines=[]
        for row in items[:3]:
            kind=row.get('kind')or'';description=row.get('description')or row.get('description_cn')or''
            lines.append(f'{row.get("name",row.get("db_id"))}（{kind}）\n{description}')
        return finish('\n\n'.join(lines)+'\n可继续到资料页看条件与来源，或加入SP清单和赛道比较。')
    entity=tool('entity.search',query=query,limit=6)
    if entity.get('items'):return finish('找到角色或衣装：\n'+'\n'.join(str(row.get('name')or row.get('character_id'))for row in entity['items'][:6]))
    return finish('这句话暂时没有匹配到明确资料。可以试“查技能＋名字”“角色＋日文名”“泥地E到A需要几星”，或先在资料查询页选候选。已输入的条件不会因此丢掉。')
