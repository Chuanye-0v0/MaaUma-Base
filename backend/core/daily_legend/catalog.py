"""Row-major order transcribed from the user's 2026-09-18 screenshots."""
NAMES = ('神鹰', '特别周', '鲁道夫象征', '帝王光辉', '大树快车', '樱花进王', '真机伶', '胜利奖券', '伏特加', '东海帝王', '重炮', '目白麦昆', '好歌剧', '超级溪流', '目白赖恩', '无声铃鹿', '黄金船', '丸善斯基', '富士奇石', '美浦波旁', '待兼福来', '青云天空', '琵琶晨光', '气槽', '荣进闪耀', '爱丽数码', '优秀素质', '草上飞', '小栗帽', '菱亚马逊', '目白多伯', '黄金城', '爱丽速子', '成田大进', '成田白仁', '目白麦昆', '米浴', '目白光明', '玉藻十字', '名将怒涛', '西野花', '菱曙', '美妙姿势', '川上公主', '目白善信', '大和赤骥')

def opponent(number):
    if type(number) is not int or not 1 <= number <= len(NAMES):
        raise ValueError('daily_legend_opponent_invalid')
    name = NAMES[number - 1]
    row, column = divmod(number - 1, 3)
    return dict(number=number, name=name, row=row, column=column, label=f'{name}（第{number}号）' if NAMES.count(name) > 1 else name)
