"""Validate current-connection subscribed symbol/type pairs reported by OpenD."""

def verify(expected, data):
    if not isinstance(data,dict) or not isinstance(data.get('sub_list'),dict):
        raise ValueError('OpenD订阅状态缺少 sub_list，不得假定订阅正常')
    actual={str(k).upper():set(map(str,v)) for k,v in data['sub_list'].items() if isinstance(v,list)}
    # SDK enum strings may render as SubType.QUOTE; normalize only the final literal.
    statuses={}
    for key in expected:
        code,typ=key.rsplit('/',1)
        name=typ.upper().split('.')[-1]
        statuses[key]='成功' if code in actual.get(name,set()) else '失败'
    return statuses
