"""Current guild's shoe requests and donations, independent of joining a guild."""
ITEMS = {'short': ('短距离跑鞋', '短距离鞋', '短距离'), 'mile': ('英里跑鞋', '英里鞋', '英里'), 'middle': ('中距离跑鞋', '中距离鞋', '中距离'), 'long': ('长距离跑鞋', '长距离鞋', '长距离'), 'dirt': ('泥地跑鞋', '泥地鞋', '沙地跑鞋', '沙地鞋', '泥地', '沙地')}

def validate_options(options):
    if not isinstance(options, dict) or set(options) - {'donate', 'request_item'}:
        raise ValueError('guild_daily_options_invalid')
    donate, item = (options.get('donate', True), options.get('request_item', 'short'))
    if item == 'none':
        item = None
    if type(donate) is not bool or (item is not None and (not isinstance(item, str) or item not in ITEMS)):
        raise ValueError('guild_daily_options_invalid')
    return dict(donate=donate, request_item=item)
