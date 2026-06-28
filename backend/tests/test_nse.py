"""
Tests for NSE bhavcopy parsing (Step 7) — OFFLINE.

We don't hit the network here (that's what spikes/ is for). Instead we hand the
parser a tiny in-memory zip in the real UDiFF bhavcopy format and check it pulls
the right EQ-series OHLC and ignores everything else. This guards against NSE
column changes silently breaking the scraper.

Runnable two ways:
  1. plain:   python -m tests.test_nse
  2. pytest:  pytest
"""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.nse_provider import _parse_bhavcopy

# Real UDiFF header (subset/order as NSE ships it) + three rows: two EQ stocks
# and one non-EQ row that must be ignored.
_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,"
    "ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,"
    "TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
_ROW_RELIANCE = "2026-06-25,2026-06-25,CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,1318.00,1328.00,1314.60,1318.10,1316.50,1313.60,,1318.10,,,12694362,1.6e10,226862,F1,1,,,,,"
_ROW_360ONE = "2026-06-25,2026-06-25,CM,NSE,STK,1,INE466L01038,360ONE,EQ,,,,,360 ONE WAM LTD,1100.0,1120.0,1090.0,1099.9,1099.0,1098.0,,1099.9,,,100,1000,50,F1,1,,,,,"
_ROW_NON_EQ = "2026-06-25,2026-06-25,CM,NSE,STK,9,INE000000000,SOMEBE,BE,,,,,SOME BE SERIES,10.0,11.0,9.0,10.5,10.4,10.3,,10.5,,,5,50,2,F1,1,,,,,"


def _make_zip() -> bytes:
    csv_text = "\n".join([_HEADER, _ROW_RELIANCE, _ROW_360ONE, _ROW_NON_EQ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BhavCopy_NSE_CM_0_0_0_20260625_F_0000.csv", csv_text)
    return buf.getvalue()


def test_parse_bhavcopy_extracts_eq_ohlc():
    parsed = _parse_bhavcopy(_make_zip())
    # Two EQ rows in, the BE row ignored.
    assert set(parsed.keys()) == {"RELIANCE", "360ONE"}, parsed.keys()
    r = parsed["RELIANCE"]
    assert (r.open, r.high, r.low, r.close) == (1318.00, 1328.00, 1314.60, 1318.10)
    o = parsed["360ONE"]
    assert (o.open, o.high, o.low, o.close) == (1100.0, 1120.0, 1090.0, 1099.9)


def test_parse_bhavcopy_empty_raises():
    """A bhavcopy with zero EQ rows means the format changed — fail loudly."""
    from app.nse_client import NSEError

    only_header = io.BytesIO()
    with zipfile.ZipFile(only_header, "w") as zf:
        zf.writestr("empty.csv", _HEADER)
    try:
        _parse_bhavcopy(only_header.getvalue())
        raise AssertionError("expected NSEError on an all-header bhavcopy")
    except NSEError:
        pass


def _run_standalone() -> int:
    checks = [
        ("parse bhavcopy extracts EQ OHLC", test_parse_bhavcopy_extracts_eq_ohlc),
        ("empty bhavcopy raises", test_parse_bhavcopy_empty_raises),
    ]
    all_ok = True
    for name, fn in checks:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            all_ok = False
            print(f"  [XX ] {name}: {e}")
    print()
    print(">>> PASS — bhavcopy parser handles the UDiFF format." if all_ok else ">>> FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
