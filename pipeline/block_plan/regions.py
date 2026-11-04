"""§4.2.1 geometry_regions：合并种子、空白带切开、相交矩形外接合并。"""


def _segments(length, blanks):
    spans = []
    i = 0
    while i < length:
        if i in blanks:
            i += 1
            continue
        j = i + 1
        while j < length and j not in blanks:
            j += 1
        spans.append((i, j))
        i = j
    return spans


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _bounds_union(rects):
    changed = True
    rects = list(rects)
    while changed:
        changed = False
        next_rects = []
        used = [False] * len(rects)
        for i, rect in enumerate(rects):
            if used[i]:
                continue
            r0, c0, r1, c1 = rect
            used[i] = True
            grew = True
            while grew:
                grew = False
                for j, other in enumerate(rects):
                    if used[j]:
                        continue
                    cur = (r0, c0, r1, c1)
                    if _intersects(cur, other):
                        used[j] = True
                        r0 = min(r0, other[0])
                        c0 = min(c0, other[1])
                        r1 = max(r1, other[2])
                        c1 = max(c1, other[3])
                        grew = True
                        changed = True
            next_rects.append((r0, c0, r1, c1))
        rects = next_rects
    return rects


def geometry_regions(grid, merges):
    rows = grid or []
    r_count = len(rows)
    c_count = max((len(r) for r in rows), default=0)
    if r_count == 0 or c_count == 0:
        return []
    width = [list(r) + [""] * (c_count - len(r)) for r in rows]
    blank_rows = {
        r for r in range(r_count) if all(not width[r][c] for c in range(c_count))
    }
    blank_cols = {
        c for c in range(c_count) if all(not width[r][c] for r in range(r_count))
    }
    covered = set()
    rects = []
    for rect in merges or []:
        r0, c0, r1, c1 = rect[0], rect[1], rect[2], rect[3]
        r0 = max(0, r0)
        c0 = max(0, c0)
        r1 = min(r_count, r1)
        c1 = min(c_count, c1)
        if r0 >= r1 or c0 >= c1:
            continue
        rects.append((r0, c0, r1, c1))
        for r in range(r0, r1):
            for c in range(c0, c1):
                covered.add((r, c))

    neighbors = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for rs, re in _segments(r_count, blank_rows):
        for cs, ce in _segments(c_count, blank_cols):
            seen = set()
            for r in range(rs, re):
                for c in range(cs, ce):
                    if (r, c) in covered or (r, c) in seen or not width[r][c]:
                        continue
                    stack = [(r, c)]
                    seen.add((r, c))
                    cells = []
                    while stack:
                        y, x = stack.pop()
                        cells.append((y, x))
                        for dy, dx in neighbors:
                            ny, nx = y + dy, x + dx
                            if not (rs <= ny < re and cs <= nx < ce):
                                continue
                            if (ny, nx) in seen or (ny, nx) in covered:
                                continue
                            if not width[ny][nx]:
                                continue
                            seen.add((ny, nx))
                            stack.append((ny, nx))
                    ys = [y for y, _x in cells]
                    xs = [x for _y, x in cells]
                    rects.append((min(ys), min(xs), max(ys) + 1, max(xs) + 1))

    if not rects:
        rects.append((0, 0, r_count, c_count))
    merged = _bounds_union(rects)
    merged.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return merged


def cut_merge_count(parts, merges):
    """一条合并范围跨入两个及以上部件，切断数 +1。"""
    cuts = 0
    for rect in merges or []:
        hit = 0
        for part in parts:
            if _intersects(rect, (part["row_span"][0], part["col_span"][0], part["row_span"][1], part["col_span"][1])):
                hit += 1
                if hit >= 2:
                    cuts += 1
                    break
    return cuts
