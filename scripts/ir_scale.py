"""Colour scale of the IR Enhanced product: pure data, no third-party imports (build_site.py uses it too).

The colours were sampled from the CIMSS/SSEC GOES-16 "Clean IR Window" (10.3 um) enhancement and are applied
to the Himawari-9 10.4 um band (B13). Brightness temperature in degrees C -> (R, G, B); warmer than the first
entry is not coloured but shown as a grey ramp, colder than the last entry keeps the last colour.
"""

CIMSS_TABLE = (
    (-20, (0, 255, 255)), (-22, (0, 204, 227)), (-24, (0, 166, 206)), (-26, (0, 115, 178)),
    (-28, (0, 64, 150)), (-30, (0, 13, 122)), (-32, (0, 21, 105)), (-34, (0, 85, 77)),
    (-36, (0, 128, 58)), (-38, (0, 191, 29)), (-40, (0, 255, 0)), (-42, (51, 255, 0)),
    (-44, (102, 255, 0)), (-46, (153, 255, 0)), (-48, (204, 255, 0)), (-50, (255, 255, 0)),
    (-52, (255, 179, 0)), (-54, (255, 153, 0)), (-56, (255, 102, 0)), (-58, (255, 77, 0)),
    (-60, (255, 26, 0)), (-62, (230, 0, 0)), (-64, (179, 0, 0)), (-66, (128, 0, 0)),
    (-68, (77, 0, 0)), (-70, (26, 0, 0)), (-72, (28, 28, 28)), (-74, (76, 76, 76)),
    (-76, (123, 133, 123)), (-78, (174, 174, 174)), (-80, (219, 219, 217)), (-82, (242, 114, 195)),
    (-84, (210, 82, 176)), (-86, (191, 64, 165)), (-88, (172, 45, 154)), (-90, (127, 0, 127)),
    (-92, (255, 255, 14)),
)
WARM_C = CIMSS_TABLE[0][0]    # -20: the warm end of the colour scale
COLD_C = CIMSS_TABLE[-1][0]   # -92: the cold end
LEGEND_TICKS_C = (-20, -30, -40, -50, -60, -70, -80, -90)

# Grey ramp for everything warmer than WARM_C (land, sea, thin cloud): black at GREY_BLACK_C, GREY_AT_WARM_C
# (of 1) at WARM_C. The reference image does not say where its grey ramp starts; +47 C is an estimate.
GREY_BLACK_C = 47.0
GREY_AT_WARM_C = 197 / 255

# Over the True Color picture (daylight) the colour is a translucent overlay: invisible at WARM_C, DAY_ALPHA
# opaque from DAY_FULL_C and colder. In darkness it is opaque wherever colder than WARM_C.
DAY_FULL_C = -40.0
DAY_ALPHA = 0.70


def css_gradient() -> str:
    """CSS linear-gradient from WARM_C (left) to COLD_C (right), for the website legend."""
    span = WARM_C - COLD_C
    stops = ", ".join(f"rgb({r},{g},{b}) {(WARM_C - t) / span * 100:.1f}%" for t, (r, g, b) in CIMSS_TABLE)
    return f"linear-gradient(to right, {stops})"
