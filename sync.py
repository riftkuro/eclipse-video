import math


def finite(value, default, minimum, maximum):
    try:
        value = float(value)
        return min(maximum, max(minimum, value)) if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def target_ms(state, now, speed=1.0, offset=0):
    position = state['position']
    if state.get('playing'):
        position += max(0, now - state['at'])
        end = state.get('endTime', 0)
        if state.get('loop') and end > 0:
            position %= end
        elif end > 0:
            position = min(position, end)
    return position * 1000 * speed + offset


def palette(raw):
    default = {'Bg': ['#132b32', '#071a20'], 'Pnl': ['#10292f'], 'TopBtn': ['#1b3f47'],
               'MenuSel': ['#557d85'], 'Txt': ['#d6e7e7'], 'Strk': ['#385b64'], 'Div': ['#294851']}
    for key in default:
        values = raw.get(key, []) if isinstance(raw, dict) else []
        clean = [v for v in values[:8] if isinstance(v, str) and len(v) == 7 and v[0] == '#' and all(c in '0123456789abcdefABCDEF' for c in v[1:])]
        if clean:
            default[key] = clean
    return default
