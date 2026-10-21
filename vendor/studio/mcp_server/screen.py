"""Grabbing the screen — one implementation, two callers.

The MCP server and the lesson recorder must see the *same* pixels: a lesson is
only replayable if the frames it was recorded from are the frames the agent
would perceive. Two copies of this would drift the moment one of them was
fixed, and the drift would show up as a lesson that resolves to the wrong
elements rather than as an error.
"""


def capture():
    """`(image, (left, top))` — the frame, and where its (0,0) actually is.

    `monitors[0]` is the union of every display, so a second monitor cannot
    silently truncate the frame. But that union's top-left is **not** the
    origin the OS uses for the cursor: the primary display is (0,0), and a
    monitor placed above or to the left of it gives the union a NEGATIVE
    `left`/`top`. Image pixel (px, py) is then cursor position
    `(left + px, top + py)`.

    Returning the offset rather than assuming it is zero is the whole point.
    Assuming it costs nothing on a single-monitor box and, on any other, sends
    every click off by a constant while every reply still reads correctly —
    the exact failure the one-coordinate-domain rule exists to prevent.
    """
    import mss
    from PIL import Image
    with mss.mss() as sct:
        m = sct.monitors[0]
        shot = sct.grab(m)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, (int(m["left"]), int(m["top"]))


def origin():
    """Where a captured frame's (0,0) sits in cursor coordinates."""
    import mss
    with mss.mss() as sct:
        m = sct.monitors[0]
    return int(m["left"]), int(m["top"])


def grab():
    """Just the pixels, for callers that never map back to the cursor."""
    return capture()[0]
